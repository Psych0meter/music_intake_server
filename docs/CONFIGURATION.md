# Configuration

## `config/scan_roots.txt`

One absolute path per line, lines starting with `#` ignored. Re-read by
the recognition daemon every ~15 seconds — no restart needed to add or
remove a folder. Files are scanned **recursively** and **in place**;
nothing moves until approved/rejected.

```
/mnt/nas-source/NewMusic
/mnt/nas-source/ToSort
```

## `config/secrets.env`

```
ACOUSTID_API_KEY=       # required - free at https://acoustid.org/my-applications
GENIUS_ACCESS_TOKEN=    # optional - only for the lyrics fallback, see below
```

Restart the daemon after editing:
```bash
systemctl restart music-recognize.service
```

## Identification pipeline in detail

| Step | Source | Cost | Fires when |
|---|---|---|---|
| 1 | SongRec (Shazam) + AcoustID/MusicBrainz | Free, unlimited | Every file |
| 2 | iTunes Search (catalog verification, not fingerprinting) | Free, no key | SongRec and AcoustID disagree |
| 3 | Local Whisper transcription + Genius lyrics search | Free, but CPU-heavy | Both of the above found *nothing at all* |

Step 3 is **not installed by default** — it needs an extra package and
meaningfully more CPU/RAM. To enable it:

```bash
sudo -u musicintake bash -c '
  source /opt/music-intake/venv/bin/activate
  pip install faster-whisper
'
```

Then set `GENIUS_ACCESS_TOKEN` in `secrets.env` and restart the daemon.
Leaving the token unset disables the fallback automatically — no code
changes needed either way.

### Tuning Whisper (if enabled)

Environment variables in `secrets.env`:

```
WHISPER_MODEL_SIZE=small     # tiny|base|small|medium|large - bigger = more accurate, slower
WHISPER_DEVICE=cpu           # or "cuda" if this LXC has GPU passthrough
WHISPER_COMPUTE_TYPE=int8    # int8 is fastest on CPU
```

## Container sizing

The default 4 vCPU / 4GB RAM / 12GB disk assumes the Whisper fallback is
enabled (it's the only CPU/RAM-heavy step; everything else is light).
If you don't plan to use it:

```bash
pct set <CTID> -cores 2 -memory 2048
```

If you do use it and have an AVX2-capable Proxmox node available,
placing this LXC there will meaningfully speed up transcription (Whisper's
CTranslate2 backend benefits significantly from AVX2).

## Bind mounts

Two separate mounts, intentionally never the same folder:

```bash
# mp0: read side - your existing library, scanned in place
pct set <CTID> -mp0 /mnt/pve/<storage>/music,mp=/mnt/nas-source

# mp1: managed output - approved/rejected/library, NOT the same folder as mp0
pct set <CTID> -mp1 /mnt/pve/<storage>/music-intake-managed,mp=/mnt/nas-intake
```

The app itself (`/opt/music-intake`) lives entirely on the LXC's local
disk — never bind-mounted — so app updates/backups are independent of
your NAS, and there's no risk of an app-side operation touching your
actual library by accident.

## AcoustID budget note

AcoustID has no hard request cap for reasonable personal use, but avoid
scanning enormous backlogs faster than a few files/second — the daemon's
15-second poll cycle already keeps this well within bounds without any
extra configuration.

## Updating

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Psych0meter/music_intake_server/main/ct/music-intake.sh)"
```

Running the same install command against an existing container detects
the prior install and pulls the latest `app/` and `pipeline/` code
without touching your config, database, or queue.

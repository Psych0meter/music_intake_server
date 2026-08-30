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

## Scan retry / change-detection behavior

A file already in the queue is only picked up again on a later poll cycle
if one of these is true:

- It's genuinely new (not in the queue yet), or
- Its on-disk modification time no longer matches the mtime recorded the
  last time it was successfully queued - i.e. you (or another process)
  actually edited/replaced the file after it was scanned.

A file that **errored** while processing (corrupt/unreadable, permission
denied, transient I/O failure, etc.) is recorded with that error and then
left alone - it is *not* auto-retried every ~15s cycle. To retry it,
either click **Rescan** on that row in the review UI (which removes it
from the queue so it's picked up as "new" on the next cycle), or fix the
file on disk, which changes its mtime and triggers automatic reprocessing.

> **Fixed bug (previously caused non-stop CPU usage / a scan that never
> finished):** `mtime` used to be captured *before* tags were written to
> the file. Since writing tags rewrites the file and bumps its real mtime,
> every successfully identified track ended up with a stored mtime that
> was already stale the moment its row was saved - so the *next* poll
> cycle always saw "changed on disk" for it and reprocessed it again,
> forever, for every tagged track in the library. `recognize.py` now
> reads `filesize`/`mtime` *after* any tag write, so the stored value
> matches what's actually on disk and a fully-scanned library correctly
> settles into an idle poll (cheap directory listing + DB compare every
> ~15s) instead of continuously reprocessing every file. The same change
> also stopped permanently-errored files from being retried every cycle
> (see above) - previously any file that couldn't be processed (e.g. a
> corrupt or 0-byte file) was retried forever as well, which compounded
> the same symptom.
>
> If you've hit this before upgrading, the queue may still contain rows
> with a stale `mtime` from before the fix, which could cause one more
> full reprocessing pass of your library after updating - that's
> expected and self-corrects; it will not loop again afterward.

## Progress bar, ETA and Purge Queue

The scanning progress bar/percentage and ETA in the review UI describe
the **current batch** of new/changed files being (re)processed, not your
whole library - once a file is queued and unchanged, it doesn't count
against "total" again, so the percentage reflects real remaining work
instead of being dominated by files that were already scanned long ago.
The ETA is a simple `files processed so far in this batch / time elapsed
since this batch started` rate projection - it firms up (and gets more
accurate) a few files into a batch, and it's inherently rough right at
the very start of a batch or right after a Pause/Resume.

**Purge Queue** deletes every row with `status = 'pending'` and stops any
scan batch that's currently running so it doesn't keep re-inserting the
rows that were just deleted (previously, purging mid-scan looked like it
"didn't clean everything," because the scan pass already in progress had
its list of files to process fixed in memory before the purge happened,
and kept writing them back regardless). Purge does **not** remove or
exclude the underlying files from `scan_roots.txt` - if they're still
there, the next poll cycle (within ~15s) will legitimately rediscover
and re-queue them, same as it would for any new file. That's expected,
not a bug: to keep specific files out of the queue for good, actually
Approve/Reject them (moves the file out of the scanned folder) or remove
their path from `scan_roots.txt`.

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

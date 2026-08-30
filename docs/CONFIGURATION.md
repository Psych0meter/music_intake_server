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

## Speeding up a large scan (SCAN_WORKERS)

`recognize.py` identifies files concurrently: `SCAN_WORKERS` files are
in flight at once, each going through SongRec/AcoustID/iTunes (and
Genius/Whisper if enabled). Nearly all of that per-file time is network/
subprocess *wait*, not CPU, so this is what actually puts extra vCPUs to
work - it's the difference between a 2000-track library taking most of a
day (one file fully done, start to finish, before the next begins) and
taking a couple of hours.

Defaults to `min(8, cpu_count)`. Override in `config/secrets.env`:

```
SCAN_WORKERS=8
```

then `systemctl restart music-recognize.service`. Things to weigh when
tuning it:

- **Raise it** if you have spare cores, a fast connection to your NAS
  source, and a big backlog to get through - the identification calls
  parallelize well since they're independent per file.
- **Lower it** if you start seeing AcoustID/iTunes/Genius errors or
  timeouts in `recognize.log` (you're outrunning their rate limits), or
  if scanning noticeably slows down NAS access for other things (you're
  saturating the mount's bandwidth/IOPS, especially over network storage
  with many concurrent reads).
- The Whisper fallback (if enabled) is the one genuinely CPU-heavy step,
  and multiple concurrent transcriptions will compete for the same
  cores - if you rely on it heavily, err toward a lower `SCAN_WORKERS`
  and more vCPUs, rather than a very high `SCAN_WORKERS` on a small core
  count.

## Container sizing

vCPU count should roughly track `SCAN_WORKERS`, since that's how many
files can genuinely be worked on at once - a high `SCAN_WORKERS` on a
low core count doesn't help much once you're saturating the box's
threads. RAM usage is modest even with many workers (a few tens of MB
per in-flight file for audio decode, well under 1GB total in most
setups) - the Whisper fallback's model is the only meaningfully sized
consumer of RAM if you enable it.

If you don't plan to use the Whisper fallback, 2-4 vCPU / 2GB RAM is
plenty for `SCAN_WORKERS` in the 4-8 range:

```bash
pct set <CTID> -cores 4 -memory 2048
```

If you do use Whisper and have an AVX2-capable Proxmox node available,
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

## Beets import behavior (config/beets-config.yaml)

`import_approved.sh` runs `beet import --quiet` against
`/mnt/nas-intake/approved/` every 30 minutes (`music-import.timer`).
Two config keys are worth understanding if the import log
(`beets-import.log`, viewable/clearable in the review UI's Logs page)
ever shows the same files being skipped run after run:

- **`duplicate_action: keep`** - the correct beets key is
  `duplicate_action`, not `duplicate`. A file that beets considers a
  duplicate of something already in the library is imported anyway
  (`keep`) rather than skipped, because duplicate detection is already
  handled upstream, before approval, in the review UI (SHA-256 filehash
  grouping, "Show Duplicates Only"). If this were left at beets'
  default (`ask`), quiet mode auto-resolves an "ask" to a silent skip
  for anything that looks like a duplicate - with no indication in the
  log beyond a bare "Skipped N paths."

- **`incremental` is deliberately left unset (beets default: off).**
  `move: yes` already gives this pipeline exactly-once import behavior
  for free - a successfully imported file is moved out of
  `/mnt/nas-intake/approved/`, so it's simply gone next time. beets'
  own `incremental` tracking is redundant on top of that, and actively
  harmful here: it permanently remembers every path it has ever
  finished a decision on, *including one it skipped* - so a file
  skipped once (for any reason, including the duplicate_action issue
  above) gets silently skipped forever after, even once the underlying
  cause is fixed, because beets never looks at it again. If you ever
  see `beets-import.log` reporting the identical "Skipped N paths."
  count run after run, this is why - `incremental` is what makes a
  skip permanent instead of self-correcting.

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

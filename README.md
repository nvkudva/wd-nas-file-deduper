# WD FileDeduper

A duplicate-file finder that runs **on** a WD My Cloud EX2 Ultra (OS 5), not over
SMB. Registers as a native app on the dashboard **Apps** page, same trick as
[WD FileBrowser App](../WD%20FileBrowser%20App) — no signed `.bin` upload needed.

Tested on firmware **5.33.102**, `armv7l`, Python **3.9.2**.

## Why it exists

No prebuilt dedupe tool ships for armv7: fclones is x86-only, Czkawka is arm64-only,
jdupes and rmlint publish no binaries. The NAS has no gcc and no package manager, so
cross-compiling was out. `dedupe.py` is **stdlib only** — `sqlite3`, `hashlib`,
`http.server`, nothing to install.

## How it finds duplicates without thrashing the disk

Scanning 2.4 TB with full-file hashes takes ~10 hours and is disk-bound. This uses a
three-stage funnel instead:

1. **Group by exact byte size** — metadata only, zero file reads.
2. **Hash first 4 MB + last 4 MB** (md5) for size collisions only.
3. **Never full-file hash.**

Groups where every entry shares one inode are hardlinks, not duplicates, and are
excluded (`COUNT(DISTINCT ino) > 1`).

Paths are stored as `BLOB` so non-UTF8 filenames survive the round trip.

### Known false positives

Stage 2 matches a prefix and a suffix, not the middle. Three classes of file collide
legitimately — check before deleting:

- **DVD `.VOB` chunks** — exactly 1.00 GB each by authoring spec.
- **TV episodes from one encode batch** — same encoder settings, same size.
- **Old XviD rips** — all padded to fit a 700 MB CD.

## The UI

Three tabs on `:8090`:

- **Folders** — duplicate folder *pairs*, picked with a radio button per side.
- **Files** — duplicate groups, collapsed by parent folder.
- **Trash** — everything staged, restorable.

Selection rules borrowed from paid dedupers (Duplicate Cleaner's Selection Assistant,
Gemini 2's Smart Select): keep shallowest/deepest path, keep newest/oldest, keep the
copy inside a chosen folder, plus a protected-path filter.

Deletion is **two-stage and reversible**: files are `os.rename`d into `app/trash/`
(same filesystem, so it is instant and copies nothing) and only leave the disk when
you empty the trash. Restore puts them back *and* re-indexes them.

The keep-one guard is computed server-side from the group's own rows, not from the
client payload — the UI cannot be coaxed into deleting every copy. Each file is
re-`lstat`ed and re-hashed immediately before it moves.

## Files (`app/`)

| File | Purpose |
|------|---------|
| `dedupe.py` | The whole app — scanner, sqlite index, HTTP server, embedded UI |
| `apkg.xml` | App manifest the dashboard reads (name, port 8090, icon) |
| `apkg.rc` | WD package metadata |
| `start.sh` | Start hook — pidfile-based, resolves `python3` by absolute path |
| `stop.sh` | Stop hook |
| `init.sh` | Install hook (no-op; stdlib only, nothing to link) |
| `remove.sh` | Uninstall hook — stops app, removes monit watch, drops registry item |
| `clean.sh` | Post-remove hook (no-op) |
| `dedupe.png` | Dashboard tile icon |
| `monit.dedupe.conf` | monit watch → boot-start + crash-restart |

## Install (on the NAS, over SSH)

```bash
NAS=sshd@<nas-ip>        # your NAS SSH user@ip
scp -r app "$NAS":/tmp/dedupe-app
ssh "$NAS" 'sh /tmp/dedupe-app/install.sh'
```

Then tunnel to it:

```bash
ssh -N -L 8090:127.0.0.1:8090 sshd@<nas-ip>
```

and open <http://localhost:8090>.

Re-running the installer is safe: it stops the app, replaces only its own files, and
leaves `dedupe.db` and `trash/` untouched.

## Uninstall

```bash
ssh "$NAS" 'sh /mnt/HD/HD_a2/Nas_Prog/dedupe/remove.sh'
```

`dedupe.db` and `trash/` are left behind on purpose. **Empty the trash from the UI
before uninstalling** if you want that space back.

## Caveats

- **Binds `127.0.0.1` by design.** Unlike filebrowser, this app has *no login* and
  exposes `/api/trash` and `/api/empty` — unauthenticated mass delete. That means the
  dashboard's **Go to app** link won't reach it; use the SSH tunnel. To expose it on
  the LAN anyway, set `DEDUPE_HOST=0.0.0.0` in `start.sh` and accept that anyone on
  your network can wipe files.
- **`pgrep -f` / `pkill -f` are unusable here** — the pattern matches the calling
  script's own command line, so `start.sh` reports a false "already running" and
  `pkill` kills the SSH session. Both hooks use a pidfile instead.
- **HTTP/1.0 + `Connection: close`** is deliberate. Keep-alive leaves stale sockets
  behind an SSH tunnel and POSTs die with `ERR_CONNECTION_RESET`.
- **No native `confirm()`/`alert()`** — suppressed silently in some browser contexts.
  Destructive buttons use a two-step in-page arm/confirm.
- **Firmware updates** (not normal reboots) wipe the registry entry and the monit
  conf, since both live on the firmware partition. Re-run the installer afterwards.
  The app directory under `Nas_Prog` is on the data volume and survives.

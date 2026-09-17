# CLAUDE.md

Working notes for developing this app. The [README](README.md) explains what it does;
this file is the stuff that cost time to discover and is invisible from the code.

## The target machine

WD My Cloud EX2 Ultra reachable at `<nas-ip>`, root SSH as `sshd@`, key auth, no password.

| | |
|---|---|
| CPU | `armv7l` (32-bit ARM, Marvell Armada) |
| Kernel | `4.14.22-armada-18.09.3` |
| OS | My Cloud OS 5, firmware `5.33.102` |
| Userland | BusyBox |
| Python | `3.9.2` at `/usr/bin/python3` → `/usr/local/modules/python39/bin/python3.9` |
| Data volume | `/mnt/HD/HD_a2` (3.6 T, ext4) |

**Not available:** gcc, any package manager, `timeout`, `sort -h`, GNU coreutils
generally. Assume BusyBox semantics for every shell builtin and flag.

This is why the app is stdlib-only Python and not a binary. No prebuilt dedupe tool
exists for armv7 — fclones is x86-only, Czkawka is arm64-only, jdupes and rmlint
publish no binaries at all. Don't go looking again; it was checked.

### Running Python on it over SSH

Nested quoting is the fiddly part. This form works — reuse it verbatim:

```
ssh -o BatchMode=yes sshd@<nas-ip> 'python3 - <<'"'"'PYEOF'"'"'
...python...
PYEOF' 2>&1 | tail -40
```

## Traps

These are all things that failed silently and cost a debugging cycle each.

**`pgrep -f` / `pkill -f` match the caller's own command line.** `pkill -f dedupe.py`
issued over SSH kills the SSH session's shell, because that shell's argv contains the
pattern. `pgrep -f dedupe.py` inside `start.sh` matches `start.sh` itself and reports
a false "already running". Both hooks use a pidfile. Never reintroduce `-f` matching.

**monit starts hooks with an empty environment.** Bare `python3` resolves over SSH
(your profile sets PATH) and fails at boot. `start.sh` hardcodes `/usr/bin/python3`
with a `command -v` fallback. Verified with `env -i`.

**HTTP keep-alive breaks behind an SSH tunnel.** `protocol_version = "HTTP/1.1"` left
stale sockets and POSTs died with `ERR_CONNECTION_RESET` — this presented as "the
trash button doesn't work". The handler is HTTP/1.0 with an explicit
`Connection: close` and the client `post()` has a `.catch()` that surfaces failures.

**Native `confirm()` and `alert()` are suppressed in some browser contexts** — they
return false with no dialog. All destructive buttons use the in-page two-step
`arm()` pattern plus `toast()`. Don't "simplify" them back to `confirm()`.

**The Basic-auth dialog is suppressed in the Claude browser pane** (you get the bare
401 body). Real Chrome/Safari prompt fine. To screenshot the UI, see below.

**Filenames are not always UTF-8.** Paths are stored as `BLOB`
(`sqlite3.Binary(os.fsencode(p))`) and decoded with `os.fsdecode`. A `TEXT` column
throws on real data in this library.

**Output width truncates paths** in terminal listings. When resolving a truncated
path back to a real file, use `glob.glob(glob.escape(prefix) + "*")`.

## How the dedupe actually works

Three-stage funnel, in `scan()` → `candidates()` → `verify()`:

1. **Group by exact byte size.** Metadata only, zero file reads.
2. **md5 of the first 4 MB + last 4 MB**, for size collisions only (`sig_of`).
3. **Never full-file hash.** 2.4 TB is ~10 hours, disk-bound at ~67 MB/s.

`GROUP_SQL` requires `COUNT(DISTINCT ino) > 1` — a group where every row shares one
inode is a hardlink, not a duplicate.

### Known false positives — do not delete these

Stage 2 matches a prefix and a suffix, never the middle. Three classes collide
legitimately and were confirmed by hand:

- **DVD `.VOB` chunks** — exactly 1.00 GB each by the DVD authoring spec.
- **TV episodes from one encode batch** — identical encoder settings, identical size.
- **Old XviD rips** — all padded to fit a 700 MB CD.

Roadmap item 4 (verify-before-empty) is the real fix: full-hash both copies at the
moment of deletion, which costs a read of files you were deleting anyway.

### Deletion is two-stage

`to_trash()` `os.rename`s into `app/trash/` — same filesystem, so it is instant and
copies nothing. Files only leave the disk on `trash_empty()`.

Two invariants worth preserving:

- The **keep-one guard is computed server-side from the group's own rows**, never
  from the client payload. The UI cannot be coaxed into deleting every copy.
- Each file is **re-`lstat`ed and re-hashed immediately before the rename**, so a
  file that changed since the scan is skipped.

`trash_restore()` renames back *and* re-inserts the `files` row with the stored
`sig`. That `sig` column on the `trash` table exists for exactly this reason —
without it, restored files reappear on disk but vanish from the views.

## Security posture

The app is on the LAN (`0.0.0.0:8090`) and exposes `/api/trash` and `/api/empty` —
unauthenticated, those are a mass-delete API. Three things hold it together:

- **HTTP Basic**, PBKDF2-SHA256, 50k rounds, 16-byte salt, in `dedupe.auth` mode 600.
  Verified `Authorization` headers are cached in memory because a derivation costs
  ~0.3 s on armv7 and the UI polls every 1.3 s.
- **A bind interlock**: `dedupe.py` exits rather than binding a non-localhost address
  when `dedupe.auth` is missing or malformed. A botched reinstall cannot quietly put
  an open delete endpoint on the network.
- **`_confined()`** resolves through symlinks before checking that a browse target is
  inside `/mnt/HD/HD_*2`. Without it the folder picker is a read-anywhere filesystem
  API behind one password.

Don't weaken any of the three. Basic auth over plain HTTP is already the accepted
compromise — it is fine on a trusted LAN and must never be port-forwarded.

## Packaging

WD's `apkg` daemon builds the dashboard Apps list from `/var/www/xml/apkg_all.xml`.
The signed-`.bin` requirement is enforced only at *upload* time, so with root SSH you
can place a normal app dir under `Nas_Prog`, inject an `<item>`, and supervise with
`monit` (the same supervisor WD uses for Plex).

- `app_id` and `<name>` must be unique. filebrowser holds `app_id 200` / `filebrowser`
  / port 8088; this app is `201` / `dedupe` / 8090. `install.sh` and `remove.sh` both
  grep on `<name>`; change both together or you will no-op one and break the other.
- **`install.sh` must keep its explicit `for f in ...` list.** Never `cp -r "$SRC"/*`:
  `dedupe.db`, the `-wal`, `dedupe.auth` and `trash/` live in that same directory and
  have to survive a reinstall.
- It stops the app before replacing `dedupe.py`, because swapping the script under a
  running process with an open WAL is an avoidable mess.
- **Firmware updates** (not reboots) wipe the registry entry and the monit conf —
  both live on the firmware partition. Re-run the installer. The app dir is on the
  data volume and survives.
- The sibling `~/WD FileBrowser App` uses the same pattern, but **its `remove.sh` is
  broken** — the awk program is double-quoted, so the shell expands `$0` before awk
  sees it. This repo's version is single-quoted. Don't copy from there.

## Screenshotting the UI

The browser pane can't save files and won't show the Basic-auth prompt. Headless
Chrome on the Mac does both:

```bash
ssh -N -L 8090:127.0.0.1:8090 sshd@<nas-ip> &   # tunnel
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --hide-scrollbars --force-dark-mode \
  --virtual-time-budget=7000 --window-size=1400,980 \
  --screenshot=docs/folders.png "http://localhost:8090/#folders"
```

The `#folders` / `#files` / `#trash` / `#browse` hashes exist partly so headless
Chrome — which cannot click — can reach every view. To capture with auth held aside,
stop the app, `mv dedupe.auth dedupe.auth.hold`, restart with
`DEDUPE_HOST=127.0.0.1`, shoot, then move it back and restart. Keep that window
short, and never leave it bound to `0.0.0.0` without the auth file.

## Working with the user's data

This NAS holds irreplaceable personal photos and video. Two standing constraints,
in their words:

> "i dont want to loose any fully downloaded movies."

> "do the redundant/duplicate checks only in the fastest way possible. if something
> is cpu heavy dont do."

And a live one: **don't trigger a volume-wide rescan without being asked.** A bounded
scan of a named subtree is fine; re-walking `/mnt/HD/HD_a2` is not. Scanning replaces
the `files` table, so an unasked-for scan also destroys whatever result set the user
was working through.

Plex indexes this volume. Anything that moves files should end with a reminder to
rescan the Plex library.

## Code map (`app/dedupe.py`, single file)

| Lines | What |
|---|---|
| ~20–35 | Config: paths, `HOST`/`PORT` from env, `SKIP_DIRS` |
| ~37–66 | `db()` — one sqlite connection per thread, WAL — and `init_db()` |
| ~68–91 | `state` table helpers driving the status line |
| ~94–104 | `sig_of()` — the head+tail hash |
| ~107–185 | `scan()`, `candidates()`, `verify()`, `scan_and_verify()` |
| ~187–241 | `GROUP_SQL`, `groups()`, `folder_sets()` (undirected folder-pair overlap) |
| ~244–343 | Trash: `to_trash()`, `trash_list()`, `trash_restore()`, `trash_empty()` |
| ~362–413 | Auth: `hash_pw()`, `load_auth()`, `check_auth()` |
| ~415–469 | Folder browser: `BROWSE_ROOTS`, `_confined()`, `browse()` |
| ~472–548 | `class H` — routes, `_authed()` gate on both `do_GET` and `do_POST` |
| ~550–end | `PAGE` — the entire UI as one embedded string |

The UI is a raw string with no build step and no dependencies. Patch it with careful
`str.replace` on a unique anchor rather than rewriting it.

# Roadmap

Next five things worth building, roughly in order of value per hour of work.

## 1. Folder browser for picking the scan root — **done**

Typing `/mnt/HD/HD_a2/Public/media/Photos` by hand is error-prone, and a
typo silently scans the wrong subtree. **Browse…** opens a picker with a breadcrumb,
`..`, and single-click descent.

Browsing is confined to the mounted data volumes (`/mnt/HD/HD_*2`) by
`_confined()` — resolving through symlinks first, so a symlink out of the volume
can't be used to turn the picker into a read-anywhere filesystem API now that the
app is on the LAN. `/etc` resolves back to the volume root instead of erroring.

## 2. Scan profiles and a scan history

One saved profile per subtree — root, min size, protected paths — so recurring
cleanups are one click instead of a re-typed form. Keep the last N scan results in
`state` so you can answer "what did this folder look like last month" without
re-walking it, and diff two scans to see what re-duplicated itself.

Needs a `scans` table and a profile dropdown next to the root input.

## 3. Content-aware image and video grouping

The current funnel matches bytes, so it misses the duplicates that actually fill a
photo library: the same shot at two export sizes, an original next to its Lightroom
render, a video and its transcode. These are *not* byte-identical and never will be.

Cheap first pass, no decoding: group by EXIF `DateTimeOriginal` + camera model, both
already in the JPEG header. A perceptual hash (dHash on a 9×8 greyscale thumbnail) is
the real fix but needs image decoding — measure it on armv7 before committing, since
that is exactly the CPU-heavy work this tool exists to avoid.

## 4. Verify-before-empty

Trash emptying is the one irreversible step, and right now it trusts the partial
hash that staged the file. Before unlinking, full-hash both the staged copy and the
survivor it was matched against, and refuse to delete any file whose partner no
longer matches or no longer exists.

This closes the `.VOB` / same-encode-batch false-positive class for good: those
collide on head and tail but differ in the middle, so a full hash separates them. It
only costs a full read of files you were about to delete anyway.

## 5. Scheduled scans with a summary

A cron entry that scans overnight and writes a dated Markdown report next to the db —
new duplicate groups, reclaimable bytes, biggest offenders. Nothing gets deleted
automatically; the report just tells you whether it is worth opening the UI.

Pairs with #2: the profile defines what to scan, the schedule decides when.

---

### Deliberately not doing

- **Full-file hashing every candidate.** ~10 hours across 2.4 TB, disk-bound at
  ~67 MB/s. That is what the head+tail funnel exists to avoid. #4 applies it only to
  the handful of files actually being deleted.
- **Hardlink-based dedup.** Replacing duplicates with hardlinks saves the space
  without the delete, but one careless edit then silently corrupts every "copy", and
  it breaks the mental model of files being independent.

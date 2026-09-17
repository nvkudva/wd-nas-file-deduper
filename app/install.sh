#!/bin/sh
# Install/register File Deduper as a WD My Cloud OS 5 dashboard app.
# Run on the NAS as root (via SSH). Idempotent: preserves dedupe.db and trash/.
set -e

APPDIR=/mnt/HD/HD_a2/Nas_Prog/dedupe
ALL=/var/www/xml/apkg_all.xml
MONIT=/etc/monit/conf-enabled/dedupe
SRC="$(cd "$(dirname "$0")" && pwd)"

# 0. Stop any running instance before replacing dedupe.py under it.
[ -x "$APPDIR/stop.sh" ] && sh "$APPDIR/stop.sh" "$APPDIR" || true

mkdir -p "$APPDIR" "$APPDIR/trash"

# 1. Install app files. Explicit list, never cp -r: dedupe.db, dedupe.db-wal
#    and trash/ live in this directory and must survive a reinstall.
for f in dedupe.py apkg.xml apkg.rc start.sh stop.sh init.sh remove.sh clean.sh dedupe.png; do
  cp -f "$SRC/$f" "$APPDIR/$f"
done
chmod +x "$APPDIR"/*.sh

# 2. Register in the dashboard app list (inject <item> if missing).
if ! grep -q "<name>dedupe</name>" "$ALL" 2>/dev/null; then
  cp -a "$ALL" "$ALL.bak.dedupe"
  sed -n '/<item>/,/<\/item>/p' "$APPDIR/apkg.xml" > /tmp/dedupe_item.xml
  awk -v item=/tmp/dedupe_item.xml '
    /<\/apkg>/ && !done { while ((getline line < item) > 0) print line; done = 1 }
    { print }' "$ALL" > /tmp/apkg_all.new
  cp -a /tmp/apkg_all.new "$ALL"
  rm -f /tmp/dedupe_item.xml /tmp/apkg_all.new
  echo "Registered in $ALL"
fi

# 3. Supervise with monit (boot-start + crash-restart).
cp -f "$SRC/monit.dedupe.conf" "$MONIT"
monit reload 2>/dev/null || true
sleep 2
monit monitor dedupe 2>/dev/null || true

# 4. Start now.
sh "$APPDIR/start.sh" "$APPDIR"

cat <<'MSG'

Done. The app binds 127.0.0.1:8090 - it has no login and can delete files,
so the dashboard "Go to app" link will not reach it. Access it over a tunnel:

    ssh -N -L 8090:127.0.0.1:8090 <user>@<nas>    # then http://localhost:8090

To expose it on the LAN anyway, set DEDUPE_HOST=0.0.0.0 in start.sh.
MSG

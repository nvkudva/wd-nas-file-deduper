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
for f in dedupe.py apkg.xml apkg.rc start.sh stop.sh init.sh remove.sh clean.sh setpw.sh dedupe.png; do
  cp -f "$SRC/$f" "$APPDIR/$f"
done
chmod +x "$APPDIR"/*.sh

# 2. Generate HTTP Basic credentials on first install. The app is reachable on
#    the LAN, so it refuses to start off-localhost without this file.
PY=/usr/bin/python3
[ -x "$PY" ] || PY="$(command -v python3)"
if [ ! -s "$APPDIR/dedupe.auth" ]; then
  "$PY" - "$APPDIR/dedupe.auth" <<'GENEOF'
import hashlib, os, secrets, sys
user = "admin"
password = secrets.token_urlsafe(12)
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 50000).hex()
with open(sys.argv[1], "w") as f:
    f.write("%s:%s:%s\n" % (user, salt.hex(), digest))
os.chmod(sys.argv[1], 0o600)
print("")
print("  =============================================")
print("   File Deduper login  (shown once - save it)")
print("     user:     %s" % user)
print("     password: %s" % password)
print("  =============================================")
print("")
GENEOF
else
  echo "Keeping existing credentials in $APPDIR/dedupe.auth"
fi
chmod 600 "$APPDIR/dedupe.auth"

# 3. Register in the dashboard app list (inject <item> if missing).
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

# 4. Supervise with monit (boot-start + crash-restart).
cp -f "$SRC/monit.dedupe.conf" "$MONIT"
monit reload 2>/dev/null || true
sleep 2
monit monitor dedupe 2>/dev/null || true

# 5. Start now.
sh "$APPDIR/start.sh" "$APPDIR"

IP="$(hostname -i 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="$(ip -4 addr 2>/dev/null | awk '/inet /&&!/127.0.0.1/{split($2,a,"/"); print a[1]; exit}')"
echo ""
echo "Done. Open http://${IP:-<nas-ip>}:8090 and log in."
echo "Change the password: printf 'newpass' | sh $APPDIR/setpw.sh"

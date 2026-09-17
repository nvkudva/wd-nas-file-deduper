#!/bin/sh
# WD apkg uninstall hook: stop app, remove monit watch, drop registry item.
# Leaves dedupe.db and trash/ in place - empty the trash from the UI first
# if you want that space back.
APPDIR=/mnt/HD/HD_a2/Nas_Prog/dedupe
ALL=/var/www/xml/apkg_all.xml

monit unmonitor dedupe 2>/dev/null
rm -f /etc/monit/conf-enabled/dedupe
monit reload 2>/dev/null

sh "$APPDIR/stop.sh" "$APPDIR"

# Single-quoted awk program: a double-quoted one lets the shell eat $0.
if grep -q "<name>dedupe</name>" "$ALL" 2>/dev/null; then
  cp -a "$ALL" "$ALL.bak.dedupe-remove"
  awk '
    /<item>/            { buf = $0; collecting = 1; next }
    collecting          { buf = buf ORS $0
                          if ($0 ~ /<\/item>/) {
                            collecting = 0
                            if (buf !~ /<name>dedupe<\/name>/) print buf
                          }
                          next }
                        { print }' "$ALL" > /tmp/apkg_all.rm && cp -a /tmp/apkg_all.rm "$ALL"
  rm -f /tmp/apkg_all.rm
  echo "Unregistered from $ALL"
fi
exit 0

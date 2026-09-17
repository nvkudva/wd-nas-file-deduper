#!/bin/sh
# WD apkg start hook. Arg $1 = install path (falls back to script dir).
# pidfile, not pgrep -f: a -f pattern would match this script's own command line.
path="$1"; [ -z "$path" ] && path="$(cd "$(dirname "$0")" && pwd)"
P="$path/dedupe.pid"
LOG="$path/dedupe.log"

# monit starts us with a minimal environment, so resolve python3 explicitly.
PY=/usr/bin/python3
[ -x "$PY" ] || PY="$(command -v python3)"
[ -x "$PY" ] || { echo "python3 not found"; exit 1; }

# Bind localhost by default: this app has no login and can delete files.
# Set DEDUPE_HOST=0.0.0.0 to expose it on the LAN (see README caveats).
DEDUPE_HOST="${DEDUPE_HOST:-127.0.0.1}"
DEDUPE_PORT="${DEDUPE_PORT:-8090}"
export DEDUPE_HOST DEDUPE_PORT

cd "$path" || exit 1
if [ -f "$P" ] && kill -0 "$(cat "$P")" 2>/dev/null; then
  case "$1" in
    restart) kill "$(cat "$P")"; sleep 2;;
    *) echo "already running ($(cat "$P"))"; exit 0;;
  esac
fi

nohup "$PY" "$path/dedupe.py" >> "$LOG" 2>&1 &
echo $! > "$P"
sleep 3
if kill -0 "$(cat "$P")" 2>/dev/null; then
  echo "started pid $(cat "$P") on $DEDUPE_HOST:$DEDUPE_PORT"
else
  echo FAILED; tail -15 "$LOG"; rm -f "$P"; exit 1
fi

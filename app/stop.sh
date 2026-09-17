#!/bin/sh
path="$1"; [ -z "$path" ] && path="$(cd "$(dirname "$0")" && pwd)"
P="$path/dedupe.pid"
[ -f "$P" ] || { echo "not running"; exit 0; }
if kill "$(cat "$P")" 2>/dev/null; then echo "stopped $(cat "$P")"; else echo "stale pidfile"; fi
rm -f "$P"

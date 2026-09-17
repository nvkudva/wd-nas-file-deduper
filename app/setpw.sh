#!/bin/sh
# Set the File Deduper password. Reads the password from stdin, never from argv,
# so it does not show up in `ps`. Restarts the app so the header cache is dropped.
#
#   printf 'mypassword' | sh setpw.sh [username]
#
set -e
APPDIR=/mnt/HD/HD_a2/Nas_Prog/dedupe
USER_NAME="${1:-admin}"
PY=/usr/bin/python3
[ -x "$PY" ] || PY="$(command -v python3)"

# -c, not a heredoc: a heredoc would occupy stdin, which carries the password.
"$PY" -c '
import hashlib, os, secrets, sys
path, user = sys.argv[1], sys.argv[2]
password = sys.stdin.read().strip()
if not password:
    sys.exit("no password on stdin")
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 50000).hex()
with open(path, "w") as f:
    f.write("%s:%s:%s\n" % (user, salt.hex(), digest))
os.chmod(path, 0o600)
print("password set for user %r" % user)
' "$APPDIR/dedupe.auth" "$USER_NAME"

sh "$APPDIR/stop.sh" "$APPDIR" >/dev/null 2>&1 || true
sh "$APPDIR/start.sh" "$APPDIR"

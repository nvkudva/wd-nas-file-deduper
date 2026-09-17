#!/usr/bin/env python3
"""Metadata-first duplicate finder for the WD NAS. stdlib only.

Funnel: group by exact byte size (no reads) -> confirm size collisions with an
md5 of the first and last 4 MB -> never hash whole files.
Deletes are staged as a same-filesystem rename into a trash dir, so they are
instant and reversible until the trash is explicitly emptied.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import sqlite3
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "dedupe.db")
TRASH = os.path.join(HERE, "trash")
HOST = os.environ.get("DEDUPE_HOST", "127.0.0.1")
PORT = int(os.environ.get("DEDUPE_PORT", "8090"))
AUTHFILE = os.path.join(HERE, "dedupe.auth")
PBKDF2_ROUNDS = 50000
CHUNK = 4 << 20
SKIP_DIRS = {".wdmc", "restsdk-data", ".systemfile", "lost+found",
             ".!@#$recycle", ".wdtmp", "trash"}

_local = threading.local()


def db():
    """One sqlite connection per thread; sqlite3 objects are not thread-safe."""
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY, path BLOB UNIQUE, size INTEGER,
        ino INTEGER, mtime INTEGER, sig TEXT);
    CREATE INDEX IF NOT EXISTS ix_files_size ON files(size);
    CREATE TABLE IF NOT EXISTS trash(
        id INTEGER PRIMARY KEY, orig BLOB, cur BLOB, size INTEGER,
        sig TEXT, ts INTEGER);
    CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(files)")]
    if "mtime" not in cols:
        c.execute("ALTER TABLE files ADD COLUMN mtime INTEGER")
    if "sig" not in [r[1] for r in c.execute("PRAGMA table_info(trash)")]:
        c.execute("ALTER TABLE trash ADD COLUMN sig TEXT")
    c.commit()


# ---------------------------------------------------------------- state ----
STATE = {"phase": "idle", "msg": "", "seen": 0, "kept": 0,
         "hashed": 0, "to_hash": 0, "root": "", "err": ""}
LOCK = threading.Lock()
BUSY = threading.Event()


def set_state(**kw):
    with LOCK:
        if "phase" in kw and kw["phase"] != STATE.get("phase") and "msg" not in kw:
            kw["msg"] = ""
        STATE.update(kw)


def snapshot():
    with LOCK:
        s = dict(STATE)
    c = db()
    s["groups"], s["waste"] = group_totals(c)
    s["trash_n"], s["trash_bytes"] = c.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM trash").fetchone()
    s["busy"] = BUSY.is_set()
    return s


# ----------------------------------------------------------------- hash ----
def sig_of(path):
    """md5 of first + last 4 MB. Bounded read regardless of file size."""
    sz = os.path.getsize(path)
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if sz > 2 * CHUNK:
            f.seek(-CHUNK, os.SEEK_END)
            h.update(f.read(CHUNK))
    return h.hexdigest()


# ----------------------------------------------------------------- scan ----
def scan(root, min_bytes):
    c = db()
    c.execute("DELETE FROM files")
    c.commit()
    set_state(phase="scanning", root=root, seen=0, kept=0,
              msg="walking the tree", err="")
    seen = kept = 0
    batch = []
    rootb = os.fsencode(root)
    for dirpath, dirs, names in os.walk(rootb):
        dirs[:] = [d for d in dirs if os.fsdecode(d) not in SKIP_DIRS]
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            seen += 1
            if st.st_size >= min_bytes:
                batch.append((sqlite3.Binary(p), st.st_size, st.st_ino,
                              int(st.st_mtime)))
                kept += 1
            if len(batch) >= 2000:
                c.executemany(
                    "INSERT OR IGNORE INTO files(path,size,ino,mtime) "
                    "VALUES(?,?,?,?)", batch)
                c.commit()
                batch = []
            if seen % 5000 == 0:
                set_state(seen=seen, kept=kept,
                          msg=os.fsdecode(dirpath)[-70:])
    if batch:
        c.executemany(
            "INSERT OR IGNORE INTO files(path,size,ino,mtime) VALUES(?,?,?,?)",
            batch)
    c.commit()
    set_state(seen=seen, kept=kept, phase="scanned", msg="scan complete")


def candidates(c):
    """Size groups with more than one distinct inode. Hardlinks excluded."""
    return [r[0] for r in c.execute(
        "SELECT size FROM files GROUP BY size "
        "HAVING COUNT(*)>1 AND COUNT(DISTINCT ino)>1")]


def verify():
    c = db()
    set_state(phase="hashing", to_hash=0, hashed=0, err="",
              msg="selecting candidate groups")
    sizes = candidates(c)
    rows = []
    for sz in sizes:
        rows += c.execute(
            "SELECT id,path FROM files WHERE size=? AND sig IS NULL",
            (sz,)).fetchall()
    set_state(to_hash=len(rows), hashed=0, msg="reading 8 MB per candidate")
    done = 0
    for fid, pb in rows:
        p = bytes(pb)
        try:
            s = sig_of(p)
        except OSError:
            s = "ERR"
        c.execute("UPDATE files SET sig=? WHERE id=?", (s, fid))
        done += 1
        if done % 25 == 0:
            c.commit()
            set_state(hashed=done, msg=os.fsdecode(p)[-70:])
    c.commit()
    set_state(hashed=done, phase="ready", msg="verification complete")


def scan_and_verify(root, min_bytes):
    scan(root, min_bytes)
    verify()


GROUP_SQL = ("SELECT size, sig, COUNT(*) n FROM files "
             "WHERE sig IS NOT NULL AND sig!='ERR' "
             "GROUP BY size, sig HAVING n>1 AND COUNT(DISTINCT ino)>1")


def group_totals(c):
    n = w = 0
    for size, _sig, cnt in c.execute(GROUP_SQL):
        n += 1
        w += size * (cnt - 1)
    return n, w


def groups(limit=400):
    c = db()
    out = []
    for size, sig, cnt in c.execute(
            GROUP_SQL + " ORDER BY size*(n-1) DESC LIMIT ?", (limit,)):
        files = [{"id": i, "path": os.fsdecode(bytes(p)), "mtime": m or 0}
                 for i, p, m in c.execute(
                     "SELECT id,path,mtime FROM files WHERE size=? AND sig=? "
                     "ORDER BY id", (size, sig))]
        out.append({"size": size, "sig": sig, "n": cnt,
                    "waste": size * (cnt - 1), "files": files})
    return out


def folder_sets(limit=60):
    """Undirected folder overlap: one entry per folder pair, both sides offered."""
    c = db()
    pairs = {}
    for size, sig, _n in c.execute(GROUP_SQL):
        byfolder = {}
        for fid, pb in c.execute(
                "SELECT id,path FROM files WHERE size=? AND sig=?", (size, sig)):
            byfolder.setdefault(
                os.path.dirname(os.fsdecode(bytes(pb))), []).append(fid)
        ds = sorted(byfolder)
        for i, a in enumerate(ds):
            for b in ds[i + 1:]:
                e = pairs.setdefault((a, b), {
                    "n": 0, "a_ids": [], "b_ids": [], "a_b": 0, "b_b": 0})
                e["n"] += 1
                e["a_ids"] += byfolder[a]
                e["a_b"] += size * len(byfolder[a])
                e["b_ids"] += byfolder[b]
                e["b_b"] += size * len(byfolder[b])
    out = []
    for (a, b), v in pairs.items():
        out.append({"n": v["n"], "bytes": max(v["a_b"], v["b_b"]), "sides": [
            {"path": a, "ids": v["a_ids"], "bytes": v["a_b"]},
            {"path": b, "ids": v["b_ids"], "bytes": v["b_b"]}]})
    out.sort(key=lambda x: -x["bytes"])
    return out[:limit]


# ---------------------------------------------------------------- trash ----
def to_trash(ids):
    """Stage deletes. Re-verifies each file and refuses to empty a group."""
    c = db()
    ids = [int(i) for i in ids]
    if not ids:
        return {"moved": 0, "freed": 0, "skipped": ["nothing selected"]}
    q = ",".join("?" * len(ids))
    rows = c.execute(
        "SELECT id,path,size,sig FROM files WHERE id IN (%s)" % q, ids
    ).fetchall()
    # keep-one guard, computed from the group's own rows not the client payload
    want = {}
    for _i, _p, size, sig in rows:
        want[(size, sig)] = want.get((size, sig), 0) + 1
    for (size, sig), k in want.items():
        total = c.execute(
            "SELECT COUNT(*) FROM files WHERE size=? AND sig=?",
            (size, sig)).fetchone()[0]
        if k >= total:
            return {"moved": 0, "freed": 0, "skipped": [
                "refused: that would delete every copy of a %.2f GB group"
                % (size / 2 ** 30)]}
    os.makedirs(TRASH, exist_ok=True)
    moved = freed = 0
    skipped = []
    for fid, pb, size, sig in rows:
        p = bytes(pb)
        disp = os.fsdecode(p)
        try:
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_size != size:
                skipped.append(disp + " (changed on disk)")
                continue
            if sig_of(p) != sig:
                skipped.append(disp + " (content changed since scan)")
                continue
        except OSError as e:
            skipped.append("%s (%s)" % (disp, e.strerror))
            continue
        dest = os.path.join(os.fsencode(TRASH),
                            os.fsencode("%d_" % fid) + os.path.basename(p))
        try:
            os.rename(p, dest)
        except OSError as e:
            skipped.append("%s (%s)" % (disp, e.strerror))
            continue
        c.execute("INSERT INTO trash(orig,cur,size,sig,ts) VALUES(?,?,?,?,?)",
                  (sqlite3.Binary(p), sqlite3.Binary(dest), size, sig,
                   int(time.time())))
        c.execute("DELETE FROM files WHERE id=?", (fid,))
        moved += 1
        freed += size
    c.commit()
    return {"moved": moved, "freed": freed, "skipped": skipped}


def trash_list():
    return [{"id": i, "path": os.fsdecode(bytes(o)), "size": s}
            for i, o, s in db().execute(
                "SELECT id,orig,size FROM trash ORDER BY id DESC LIMIT 500")]


def trash_restore(ids):
    c = db()
    n = 0
    for tid in [int(i) for i in ids]:
        r = c.execute("SELECT orig,cur,size,sig FROM trash WHERE id=?",
                      (tid,)).fetchone()
        if not r:
            continue
        orig, cur, size, sig = bytes(r[0]), bytes(r[1]), r[2], r[3]
        try:
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            os.rename(cur, orig)
            st = os.lstat(orig)
        except OSError:
            continue
        c.execute("INSERT OR REPLACE INTO files(path,size,ino,mtime,sig) "
                  "VALUES(?,?,?,?,?)", (sqlite3.Binary(orig), size, st.st_ino,
                                        int(st.st_mtime), sig))
        c.execute("DELETE FROM trash WHERE id=?", (tid,))
        n += 1
    c.commit()
    return {"restored": n}


def trash_empty():
    c = db()
    n = freed = 0
    for tid, cur, size in c.execute("SELECT id,cur,size FROM trash").fetchall():
        try:
            os.unlink(bytes(cur))
        except OSError:
            pass
        c.execute("DELETE FROM trash WHERE id=?", (tid,))
        n += 1
        freed += size
    c.commit()
    return {"deleted": n, "freed": freed}


# ------------------------------------------------------------------ web ----
def run_bg(fn, *a):
    if BUSY.is_set():
        return False

    def work():
        BUSY.set()
        try:
            fn(*a)
        except Exception as e:
            set_state(phase="error", err="%s: %s" % (type(e).__name__, e))
        finally:
            BUSY.clear()
    threading.Thread(target=work, daemon=True).start()
    return True


# ---------------------------------------------------------------- auth
# dedupe.auth holds one line: user:salt_hex:pbkdf2_hex
# pbkdf2 on armv7 costs ~0.3 s, and the UI polls every 1.3 s, so verified
# Authorization headers are cached rather than re-derived per request.
_auth_cache = set()
_auth_lock = threading.Lock()


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


def load_auth():
    """Return (user, salt_bytes, hexdigest), or None if no credentials are set."""
    try:
        line = open(AUTHFILE).read().strip()
    except IOError:
        return None
    parts = line.split(":")
    if len(parts) != 3 or not all(parts):
        return None
    user, salt_hex, digest = parts
    try:
        return user, bytes.fromhex(salt_hex), digest
    except ValueError:
        return None


def check_auth(header):
    """Validate an Authorization header against dedupe.auth."""
    cred = load_auth()
    if cred is None:
        return True                       # no credentials configured
    if not header or not header.startswith("Basic "):
        return False
    with _auth_lock:
        if header in _auth_cache:
            return True
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
        user, _, password = raw.partition(":")
    except Exception:
        return False
    want_user, salt, want_digest = cred
    ok = (hmac.compare_digest(user, want_user)
          and hmac.compare_digest(hash_pw(password, salt), want_digest))
    if ok:
        with _auth_lock:
            _auth_cache.add(header)
    return ok


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(json.dumps(obj), "application/json")

    def _authed(self):
        if check_auth(self.headers.get("Authorization")):
            return True
        body = b"authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="File Deduper"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        if not self._authed():
            return
        p = self.path.split("?")[0]
        if p == "/":
            return self._send(PAGE, "text/html; charset=utf-8")
        if p == "/api/status":
            return self._json(snapshot())
        if p == "/api/groups":
            return self._json(groups())
        if p == "/api/folders":
            return self._json(folder_sets())
        if p == "/api/trash":
            return self._json(trash_list())
        self.send_error(404)

    def do_POST(self):
        if not self._authed():
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        p = self.path.split("?")[0]
        if p == "/api/scan":
            root = body.get("root") or "/mnt/HD/HD_a2"
            mb = float(body.get("min_mb") or 1)
            if not os.path.isdir(root):
                return self._json({"error": "no such directory: " + root})
            ok = run_bg(scan_and_verify, root, int(mb * 2 ** 20))
            return self._json({"started": ok})
        if p == "/api/verify":
            return self._json({"started": run_bg(verify)})
        if p == "/api/trash":
            return self._json(to_trash(body.get("ids") or []))
        if p == "/api/restore":
            return self._json(trash_restore(body.get("ids") or []))
        if p == "/api/empty":
            return self._json(trash_empty())
        self.send_error(404)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>NAS Dedupe</title><style>
*{box-sizing:border-box}
body{font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
margin:0;background:#0f141b;color:#dde5ef}
header{padding:12px 20px 0;background:#151b25;border-bottom:1px solid #222c3b;
position:sticky;top:0;z-index:20}
h1{margin:0 0 9px;font-size:15px;font-weight:600;letter-spacing:.2px}
input,button,select{font:inherit;padding:5px 9px;border-radius:6px;
border:1px solid #2b3648;background:#0d131c;color:#dde5ef}
input:focus,select:focus{outline:2px solid #2d6cdf60;border-color:#2d6cdf}
button{background:#2d6cdf;border-color:#2d6cdf;color:#fff;cursor:pointer}
button:hover:not(:disabled){filter:brightness(1.15)}
button.ghost{background:#1a2230;border-color:#2b3648;color:#c2cedd}
button.warn{background:#b23f2e;border-color:#b23f2e}
button.sm{padding:3px 8px;font-size:12px}
button:disabled{opacity:.4;cursor:default}
.row{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
#stat{margin:8px 0;color:#8b9cb3;font-size:12.5px;min-height:17px}
#stat b{color:#dde5ef}
.tabs{display:flex;gap:2px;margin-top:4px}
.tab{padding:7px 15px;cursor:pointer;border-radius:6px 6px 0 0;font-size:13px;
color:#8b9cb3;border:1px solid transparent;border-bottom:none}
.tab.on{background:#0f141b;color:#fff;border-color:#222c3b}
main{padding:16px 20px 70px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:#8b9cb3;font-weight:500;padding:6px 9px;
border-bottom:1px solid #222c3b;font-size:11.5px;text-transform:uppercase;
letter-spacing:.4px}
td{padding:7px 9px;border-bottom:1px solid #19212c;vertical-align:top}
tr:hover td{background:#141b25}
.path{font-family:ui-monospace,Menlo,monospace;word-break:break-all}
.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.gb{color:#ffc76b;font-weight:600}
.g{background:#131a24;border:1px solid #202a38;border-radius:8px;
margin-bottom:9px;overflow:hidden}
.gh{padding:7px 12px;background:#182029;display:flex;gap:10px;
align-items:baseline;font-size:12.5px;flex-wrap:wrap}
.gh .pre{font-family:ui-monospace,Menlo,monospace;color:#7f8fa6;font-size:11.5px}
.f{display:flex;gap:9px;padding:4px 12px;align-items:center;
border-top:1px solid #1b2430;font-family:ui-monospace,Menlo,monospace;
font-size:12px;word-break:break-all}
.f label{cursor:pointer;flex:1}
.f.kept label{color:#6ed08d}
.f.prot label{color:#7f8fa6;text-decoration:line-through}
.tag{font-size:10px;padding:1px 6px;border-radius:9px;background:#222e41;
color:#9db0c7;white-space:nowrap;text-transform:uppercase;letter-spacing:.4px}
.tag.k{background:#1d3a28;color:#6ed08d}
.tag.p{background:#3a2a1d;color:#e0a95f}
.bar{background:#131a24;border:1px solid #202a38;border-radius:8px;
padding:10px 12px;margin-bottom:12px}
.bar .lbl{color:#8b9cb3;font-size:12px}
.empty{color:#7f8fa6;padding:22px;text-align:center}
.sec{margin:16px 0 7px;padding:7px 11px;background:#101823;border-left:3px solid
#2d6cdf;border-radius:0 6px 6px 0;font-size:12px;display:flex;gap:8px;
align-items:center;flex-wrap:wrap}
.secf{font-family:ui-monospace,Menlo,monospace;color:#bcccdf;word-break:break-all}
.secx{color:#5d6b80}
.secn{margin-left:auto;color:#8b9cb3;white-space:nowrap}
#toast{display:none;position:fixed;left:50%;transform:translateX(-50%);
bottom:64px;padding:10px 18px;border-radius:8px;color:#fff;font-size:13px;
z-index:50;box-shadow:0 6px 24px #0008;max-width:80vw}
button.armed{background:#e0a33f;border-color:#e0a33f;color:#1a1200;
font-weight:600}
.sticky{position:sticky;bottom:0;background:#151b25e8;backdrop-filter:blur(6px);
border-top:1px solid #222c3b;padding:9px 20px;display:flex;gap:9px;
align-items:center;margin:0 -20px -70px;z-index:10}
#selinfo{color:#8b9cb3;font-size:12.5px;flex:1}
</style></head><body>
<header>
<h1>NAS Dedupe <span class="tag">size &rarr; 4 MB head+tail hash</span></h1>
<div class="row">
<input id="root" size="44" value="/mnt/HD/HD_a2/Public/media/Photos">
<span class="lbl">min MB</span><input id="min" size="3" value="50">
<button id="bscan">Scan + verify</button>
<button id="bver" class="ghost" title="Re-hash without rescanning">Verify only</button>
</div>
<div id="stat">loading&hellip;</div>
<div class="tabs">
<div class="tab on" data-t="folders">Folders</div>
<div class="tab" data-t="files">Files</div>
<div class="tab" data-t="trash">Trash</div>
</div>
</header>
<main>

<div id="v-folders">
<div class="bar"><span class="lbl">Folders that share content. Pick the side to
remove &mdash; the other is kept, and only the shared files are touched.</span>
</div>
<div id="fsets"></div>
</div>

<div id="v-files" style="display:none">
<div class="bar row">
<span class="lbl">Keep the copy that is</span>
<select id="rule">
<option value="short">shallowest path</option>
<option value="long">deepest path</option>
<option value="new">newest</option>
<option value="old">oldest</option>
<option value="in">in a folder matching&hellip;</option>
</select>
<input id="rulein" size="26" placeholder="e.g. Lightroom" style="display:none">
<span class="lbl">&nbsp;|&nbsp; never touch paths containing</span>
<input id="prot" size="22" placeholder="(nothing protected)">
<button id="bapply" class="ghost">Apply</button>
<button id="bclear" class="ghost">Clear</button>
</div>
<div id="groups"></div>
</div>

<div id="v-trash" style="display:none">
<div class="bar row"><span class="lbl">Staged deletes. Files were renamed, not
removed &mdash; restoring is instant.</span>
<button id="brest" class="ghost">Restore all</button>
<button id="bempty" class="warn">Empty trash (permanent)</button></div>
<table><thead><tr><th class="num">Size</th><th>Original location</th></tr>
</thead><tbody id="ttb"></tbody></table>
</div>

<div id="toast"></div>
<div class="sticky" id="selbar" style="display:none">
<span id="selinfo"></span>
<button id="bdel" class="warn">Move selected to trash</button>
</div>
</main>
<script>
var G=[],F=[],TAB="folders",BUSY=false;
function gb(b){return (b/1073741824).toFixed(2)+" GB";}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){
return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function $(i){return document.getElementById(i);}
function post(u,o){return fetch(u,{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(o||{})})
.then(function(r){return r.json();})
.catch(function(e){toast("Request failed: "+e.message+" &mdash; is the tunnel up?",1);
 throw e;});}
function short(p){return p.replace("/mnt/HD/HD_a2/","");}
function toast(msg,bad){
 var d=$("toast"); d.innerHTML=msg;
 d.style.background=bad?"#b23f2e":"#1d6b3a"; d.style.display="block";
 clearTimeout(d._t); d._t=setTimeout(function(){d.style.display="none";},7000);}
function report(r){
 if(r.error)return toast(esc(r.error),1);
 if(r.moved) toast("Moved "+r.moved+" file(s) to trash, freed "+gb(r.freed));
 else if(r.restored!==undefined) toast("Restored "+r.restored+" file(s)");
 else if(r.deleted!==undefined) toast("Deleted "+r.deleted+" file(s), freed "+gb(r.freed));
 if(r.skipped&&r.skipped.length)
  toast(esc(r.skipped.join(" | ")),1);}
/* two-step confirm: first click arms the button, second click fires */
function arm(btn,label,fn){
 if(btn._armed){clearTimeout(btn._at);btn._armed=false;btn.textContent=btn._orig;
  btn.classList.remove("armed");fn();return;}
 btn._orig=btn.textContent; btn._armed=true; btn.classList.add("armed");
 btn.textContent="Click again: "+label;
 btn._at=setTimeout(function(){btn._armed=false;btn.textContent=btn._orig;
  btn.classList.remove("armed");},6000);}

/* ---- tabs ---- */
document.querySelectorAll(".tab").forEach(function(t){
 t.onclick=function(){
  document.querySelectorAll(".tab").forEach(function(x){
    x.classList.toggle("on",x===t);});
  TAB=t.dataset.t;
  ["folders","files","trash"].forEach(function(n){
    $("v-"+n).style.display = n===TAB ? "" : "none";});
  $("selbar").style.display = TAB==="files" ? "flex" : "none";
  load();};});

/* ---- status ---- */
function tick(){
 fetch("/api/status").then(function(r){return r.json();}).then(function(s){
  var NAME={idle:"Idle",scanning:"Scanning",scanned:"Scan complete",
   hashing:"Hashing",ready:"Ready",error:"Error"};
  var t="<b>"+(NAME[s.phase]||s.phase)+"</b>";
  if(s.busy) t+=" <span class='tag'>working</span>";
  if(s.phase==="scanning") t+=" &middot; "+s.seen.toLocaleString()+" files seen, "
    +s.kept.toLocaleString()+" over threshold";
  if(s.phase==="hashing") t+=" &middot; hashed "+s.hashed+" / "+s.to_hash;
  if(s.msg) t+=" &middot; <span style='opacity:.65'>"+esc(s.msg)+"</span>";
  if(s.err) t+=" &middot; <span style='color:#ff8b7a'>"+esc(s.err)+"</span>";
  t+="<br><b>"+s.groups+"</b> confirmed groups &middot; <b class='gb'>"
    +gb(s.waste)+"</b> reclaimable &middot; trash "+s.trash_n+" files ("
    +gb(s.trash_bytes)+")";
  $("stat").innerHTML=t;
  $("bscan").disabled=s.busy; $("bver").disabled=s.busy;
  if(BUSY&&!s.busy) load();
  BUSY=s.busy;
 }).catch(function(){});
}

/* ---- data ---- */
function load(){
 if(TAB==="folders")
  fetch("/api/folders").then(function(r){return r.json();}).then(function(f){
   F=f; renderFolders();});
 if(TAB==="files")
  fetch("/api/groups").then(function(r){return r.json();}).then(function(g){
   G=g; renderGroups();});
 if(TAB==="trash")
  fetch("/api/trash").then(function(r){return r.json();}).then(function(t){
   $("ttb").innerHTML=t.map(function(f){
    return "<tr><td class='num'>"+gb(f.size)+"</td><td class='path'>"
     +esc(short(f.path))+"</td></tr>";}).join("")
    ||"<tr><td colspan=2 class='empty'>Trash is empty.</td></tr>";});
}

function renderFolders(){
 $("fsets").innerHTML=F.map(function(p,i){
  return "<div class='g'><div class='gh'>"+p.n+" shared file"
   +(p.n>1?"s":"")+" &middot; <span class='gb'>"+gb(p.bytes)
   +"</span> &middot; <span style='color:#8b9cb3'>select the copy to remove"
   +"</span></div>"
   +p.sides.map(function(sd,j){
     return "<div class='f'><input type='radio' name='fs"+i+"' data-i='"+i
      +"' data-j='"+j+"'><label onclick=\"var r=this.previousElementSibling;"
      +"r.checked=true;fmark();\">"+esc(short(sd.path))+"</label>"
      +"<span class='tag'>"+sd.ids.length+" files &middot; "+gb(sd.bytes)
      +"</span></div>";}).join("")
   +"<div class='f' style='justify-content:flex-end;gap:8px'>"
   +"<span class='lbl' id='fi"+i+"'>nothing selected</span>"
   +"<button class='sm warn' id='fb"+i+"' disabled>Trash folder</button></div>"
   +"</div>";}).join("")
  ||"<div class='empty'>No overlapping folders yet. "
   +"Run a scan &mdash; hashing follows automatically.</div>";
 $("fsets").querySelectorAll("input[type=radio]").forEach(function(r){
   r.onchange=fmark;});
 F.forEach(function(p,i){
  $("fb"+i).onclick=function(){
   var r=document.querySelector("input[name='fs"+i+"']:checked");
   if(!r)return;
   var sd=p.sides[+r.dataset.j];
   arm(this,"trash "+sd.ids.length+" from "+sd.path.split("/").pop(),
    function(){post("/api/trash",{ids:sd.ids}).then(function(r2){
     report(r2); load();});});};});
 fmark();
}

function fmark(){
 F.forEach(function(p,i){
  var r=document.querySelector("input[name='fs"+i+"']:checked");
  var b=$("fb"+i), info=$("fi"+i);
  if(!b)return;
  b.disabled=!r;
  if(r){var sd=p.sides[+r.dataset.j];
   info.innerHTML="removes "+sd.ids.length+" file(s), frees <b class='gb'>"
    +gb(sd.bytes)+"</b>";
   b.textContent="Trash "+sd.ids.length;}
  else {info.textContent="nothing selected"; b.textContent="Trash folder";}
  [].slice.call(document.getElementsByName("fs"+i)).forEach(function(x){
   x.parentElement.classList.toggle("kept", r && x!==r);});
 });
}

function commonPrefix(paths){
 var a=paths[0].split("/"),n=a.length;
 paths.forEach(function(p){var b=p.split("/"),i=0;
  while(i<n&&i<b.length&&a[i]===b[i])i++; n=i;});
 return a.slice(0,n).join("/");
}

function folderSig(x){
 var d={}; x.files.forEach(function(f){
  d[f.path.slice(0,f.path.lastIndexOf("/"))]=1;});
 return Object.keys(d).sort().join(" \u00b7 ");
}

function renderGroups(){
 var prot=$("prot").value.trim();
 var order=G.map(function(x,i){return i;}).sort(function(a,b){
  var sa=folderSig(G[a]),sb=folderSig(G[b]);
  if(sa!==sb)return sa<sb?-1:1;
  return G[b].waste-G[a].waste;});
 var tot={},cnt={};
 order.forEach(function(i){var k=folderSig(G[i]);
  tot[k]=(tot[k]||0)+G[i].waste; cnt[k]=(cnt[k]||0)+1;});
 var last=null;
 $("groups").innerHTML=order.map(function(gi){
  var x=G[gi], sig=folderSig(x), head="";
  if(sig!==last){last=sig;
   head="<div class='sec'>"+sig.split(" \u00b7 ").map(function(d){
     return "<span class='secf'>"+esc(short(d))+"</span>";}).join(
     "<span class='secx'>&harr;</span>")
    +"<span class='secn'>"+cnt[sig]+" group"+(cnt[sig]>1?"s":"")
    +" &middot; <span class='gb'>"+gb(tot[sig])+"</span></span></div>";}
  return head+(function(x,gi){
  var paths=x.files.map(function(f){return f.path;});
  var pre=x.files.length>1?commonPrefix(paths):"";
  return "<div class='g'><div class='gh'>"+gb(x.size)+" each &middot; "+x.n
   +" copies &middot; <span class='gb'>"+gb(x.waste)+" reclaimable</span>"
   +(pre?" <span class='pre'>"+esc(short(pre))+"/&hellip;</span>":"")
   +"</div>"+x.files.map(function(f,i){
     var isP=prot&&f.path.indexOf(prot)>=0;
     var tail=pre?f.path.slice(pre.length+1):short(f.path);
     return "<div class='f' id='r"+f.id+"'><input type='checkbox' class='cb' "
      +"data-g='"+gi+"' value='"+f.id+"'"+(isP?" disabled":"")+">"
      +"<label for='' onclick=\"var c=this.previousElementSibling;"
      +"if(!c.disabled){c.checked=!c.checked;mark();}\">"+esc(tail)+"</label>"
      +(isP?"<span class='tag p'>protected</span>":"")+"</div>";}).join("")
   +"</div>";})(x,gi);}).join("")
  ||"<div class='empty'>No confirmed groups yet. Run a scan &mdash; "
   +"hashing follows automatically.</div>";
 document.querySelectorAll(".cb").forEach(function(c){c.onchange=mark;});
 mark();
}

function mark(){
 var n=0,b=0,byg={};
 document.querySelectorAll(".cb").forEach(function(c){
  var row=c.parentElement;
  row.classList.toggle("kept",!c.checked&&!c.disabled);
  if(c.checked){n++; byg[c.dataset.g]=(byg[c.dataset.g]||0)+1;
   b+=G[+c.dataset.g].size;}});
 var bad=Object.keys(byg).filter(function(g){
   return byg[g]>=G[+g].files.length;});
 $("selinfo").innerHTML = n ? n+" selected &middot; frees <b class='gb'>"+gb(b)
   +"</b>"+(bad.length?" &middot; <span style='color:#ff8b7a'>"+bad.length
   +" group(s) fully selected &mdash; the server will refuse those</span>":"")
   : "Nothing selected.";
 $("bdel").disabled = !n;
}

function applyRule(){
 var r=$("rule").value, txt=$("rulein").value.trim(), prot=$("prot").value.trim();
 G.forEach(function(x,gi){
  var idx=0, fs=x.files;
  if(r==="short") fs.forEach(function(f,i){
    if(f.path.split("/").length<fs[idx].path.split("/").length)idx=i;});
  if(r==="long") fs.forEach(function(f,i){
    if(f.path.split("/").length>fs[idx].path.split("/").length)idx=i;});
  if(r==="new") fs.forEach(function(f,i){if(f.mtime>fs[idx].mtime)idx=i;});
  if(r==="old") fs.forEach(function(f,i){if(f.mtime<fs[idx].mtime)idx=i;});
  if(r==="in"&&txt){var hit=-1;
    fs.forEach(function(f,i){if(hit<0&&f.path.indexOf(txt)>=0)hit=i;});
    idx = hit<0 ? -1 : hit;}
  fs.forEach(function(f,i){
   var c=document.querySelector(".cb[value='"+f.id+"']");
   if(!c||c.disabled)return;
   c.checked = idx>=0 && i!==idx;});
 });
 mark();
}

/* ---- actions ---- */
$("rule").onchange=function(){
 $("rulein").style.display=this.value==="in"?"":"none";};
$("bapply").onclick=applyRule;
$("bclear").onclick=function(){
 document.querySelectorAll(".cb").forEach(function(c){c.checked=false;});mark();};
$("prot").oninput=function(){renderGroups();};
$("bscan").onclick=function(){
 arm(this,"rescan (clears current results)",function(){
  post("/api/scan",{root:$("root").value,min_mb:$("min").value})
  .then(report);});};
$("bver").onclick=function(){post("/api/verify");};
$("bdel").onclick=function(){
 var s=[].slice.call(document.querySelectorAll(".cb:checked"))
   .map(function(c){return +c.value;});
 if(!s.length)return;
 arm(this,"trash "+s.length+" file(s)",function(){
  post("/api/trash",{ids:s}).then(function(r){report(r);load();});});};
$("brest").onclick=function(){
 fetch("/api/trash").then(function(r){return r.json();}).then(function(t){
  post("/api/restore",{ids:t.map(function(f){return f.id;})})
   .then(function(r){report(r);load();});});};
$("bempty").onclick=function(){
 arm(this,"PERMANENTLY delete all trash",function(){
  post("/api/empty").then(function(r){report(r);load();});});};

setInterval(tick,1300); tick(); load();
</script></body></html>
"""


if __name__ == "__main__":
    if HOST not in ("127.0.0.1", "localhost") and load_auth() is None:
        sys.exit("refusing to bind %s with no credentials: %s is missing or "
                 "malformed. Run install.sh, or set DEDUPE_HOST=127.0.0.1."
                 % (HOST, AUTHFILE))
    init_db()
    os.makedirs(TRASH, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), H)
    srv.daemon_threads = True
    print("dedupe on http://%s:%d  db=%s" % (HOST, PORT, DB))
    sys.stdout.flush()
    srv.serve_forever()

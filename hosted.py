#!/usr/bin/env python3
"""Hosted Stepwhere Sales Desk: user logins, shared database, background sync.

Runs on the AWS server behind Caddy (which provides HTTPS). The Shopify and
Amazon code in server.py is used unchanged; this file only replaces where the
data is kept (SQLite instead of the browser) and who may connect (people with
a login instead of anyone on this computer).

Commands:
    python3 hosted.py                 start the web server
    python3 hosted.py adduser NAME    add a user, or reset that user's password
    python3 hosted.py deluser NAME    remove a user and sign them out
    python3 hosted.py users           list users
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import server as market

ROOT = market.ROOT
SHIM_FILE = ROOT / "shim.js"
COOKIE = "sd_session"
SESSION_SECONDS = 30 * 24 * 3600
MAX_BODY = 25 * 1024 * 1024
SYNC_THROTTLE_SECONDS = 30      # a browser-triggered sync is skipped if one ran this recently
HISTORY_EVERY_SECONDS = 3600    # keep a safety copy of the shared data at most once an hour
HISTORY_KEEP = 48
LOGIN_MAX_FAILS = 8
LOGIN_WINDOW_SECONDS = 900

DB_LOCK = threading.RLock()
SESSION_CURSORS = {}            # session hash -> {"acked": seq, "pending": seq}
LOGIN_FAILS = {}                # ip -> [timestamps]


# ----------------------------------------------------------------- database

def data_dir():
    return Path(os.getenv("DATA_DIR") or (ROOT / "data"))


def connect():
    conn = sqlite3.connect(data_dir() / "sales.db", timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def run(sql, args=(), write=False):
    with DB_LOCK:
        conn = connect()
        try:
            rows = conn.execute(sql, args).fetchall()
            if write:
                conn.commit()
            return rows
        finally:
            conn.close()


def init_db():
    data_dir().mkdir(parents=True, exist_ok=True)
    with DB_LOCK:
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, pw TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS app_state(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
                    data TEXT NOT NULL, updated TEXT NOT NULL, updated_by TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state_history(version INTEGER PRIMARY KEY, data TEXT NOT NULL, saved REAL NOT NULL, saved_by TEXT);
                CREATE TABLE IF NOT EXISTS sync_orders(id TEXT PRIMARY KEY, seq INTEGER NOT NULL, data TEXT NOT NULL);
            """)
            conn.commit()
        finally:
            conn.close()


def kv_get(key):
    rows = run("SELECT value FROM kv WHERE key=?", (key,))
    return rows[0]["value"] if rows else None


def kv_set(key, value):
    run("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value), write=True)


# server.py keeps its sync cursors in a file; here they live in the database.
def read_state():
    raw = kv_get("sync_state")
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def write_state(data):
    kv_set("sync_state", json.dumps(data))


market.read_state = read_state
market.write_state = write_state


# ------------------------------------------------------------ shared app data

def get_state():
    rows = run("SELECT version, data, updated_by FROM app_state WHERE id=1")
    if not rows:
        return 0, None, ""
    return rows[0]["version"], rows[0]["data"], rows[0]["updated_by"]


def put_state(base_version, data, username):
    """Save the shared data only if the caller saw the latest version.
    Returns (saved, current_version)."""
    now = time.time()
    with DB_LOCK:
        conn = connect()
        try:
            row = conn.execute("SELECT version, data, updated_by FROM app_state WHERE id=1").fetchone()
            current = row["version"] if row else 0
            if base_version != current:
                return False, current
            new_version = current + 1
            stamp = datetime.now(timezone.utc).isoformat()
            if row:
                last = conn.execute("SELECT MAX(saved) AS m FROM state_history").fetchone()["m"] or 0
                if now - last > HISTORY_EVERY_SECONDS:
                    conn.execute("INSERT OR REPLACE INTO state_history(version,data,saved,saved_by) VALUES(?,?,?,?)",
                                 (current, row["data"], now, row["updated_by"]))
                    conn.execute("DELETE FROM state_history WHERE version NOT IN "
                                 "(SELECT version FROM state_history ORDER BY version DESC LIMIT ?)", (HISTORY_KEEP,))
                conn.execute("UPDATE app_state SET version=?, data=?, updated=?, updated_by=? WHERE id=1",
                             (new_version, data, stamp, username))
            else:
                conn.execute("INSERT INTO app_state(id,version,data,updated,updated_by) VALUES(1,?,?,?,?)",
                             (new_version, data, stamp, username))
            conn.commit()
            return True, new_version
        finally:
            conn.close()


# ---------------------------------------------------------- users and sessions

def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


DUMMY_HASH = hash_password(secrets.token_urlsafe(12))  # keeps timing the same for unknown users


def valid_username(name):
    return re.fullmatch(r"[a-z0-9._@-]{2,60}", name) is not None


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session(username):
    token = secrets.token_urlsafe(32)
    run("DELETE FROM sessions WHERE expires < ?", (time.time(),), write=True)
    run("INSERT INTO sessions(token_hash,username,expires) VALUES(?,?,?)",
        (token_hash(token), username, time.time() + SESSION_SECONDS), write=True)
    return token


def lookup_session(token):
    th = token_hash(token)
    rows = run("SELECT s.username FROM sessions s JOIN users u ON u.username=s.username "
               "WHERE s.token_hash=? AND s.expires>?", (th, time.time()))
    return (rows[0]["username"], th) if rows else None


# ------------------------------------------------------------------- syncing

def store_synced_orders(orders):
    """Keep the latest copy of each marketplace order. Only new or changed
    orders get a new sequence number, so browsers are sent just those."""
    with DB_LOCK:
        conn = connect()
        try:
            seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM sync_orders").fetchone()[0]
            for order in orders:
                oid = str(order.get("id") or "")
                if not oid:
                    continue
                blob = json.dumps(order, sort_keys=True)
                row = conn.execute("SELECT data FROM sync_orders WHERE id=?", (oid,)).fetchone()
                if row and row[0] == blob:
                    continue
                seq += 1
                conn.execute("INSERT OR REPLACE INTO sync_orders(id,seq,data) VALUES(?,?,?)", (oid, seq, blob))
            conn.commit()
        finally:
            conn.close()


def do_sync(channel):
    result = market.run_sync(channel)
    store_synced_orders(result["orders"])
    kv_set("last_errors", json.dumps(result["errors"]))
    return result


def last_sync_age():
    stamp = market.parse_iso(read_state().get("last_sync"))
    if not stamp:
        return None
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def background_loop(interval):
    time.sleep(5)
    while True:
        try:
            do_sync("all")
        except Exception as exc:  # keep the loop alive whatever happens
            print(f"Background sync failed: {exc}", flush=True)
        time.sleep(interval)


# --------------------------------------------------------------------- pages

def render_index():
    page = market.INDEX_FILE.read_text(encoding="utf-8")
    tag = '<script src="/shim.js"></script>'
    match = re.search(r"<head[^>]*>", page, re.I)
    return page[:match.end()] + tag + page[match.end():] if match else tag + page


LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Stepwhere Sales Desk</title>
<style>
:root{--bg:#0e1117;--panel:#151a23;--line:#262d3a;--text:#e8ebf1;--muted:#8d97a8;--amber:#f5a524;--warm:#ff6a3d;--bad:#ff7a7a}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:24px}
main{width:100%;max-width:380px}
.mark{font-weight:800;font-size:22px;letter-spacing:.14em;margin:0 0 28px}
.mark b{background:linear-gradient(90deg,#ff4d8d,var(--warm));-webkit-background-clip:text;background-clip:text;color:transparent}
h1{font-size:24px;margin:0 0 4px}
p.sub{color:var(--muted);margin:0 0 24px}
form{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px}
label{display:block;font-size:13px;color:var(--muted);margin:0 0 6px}
input{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:9px;padding:11px 12px;font:inherit;margin-bottom:16px}
input:focus-visible{outline:2px solid var(--amber);outline-offset:1px;border-color:transparent}
button{width:100%;background:var(--amber);color:#1a1200;border:0;border-radius:9px;padding:12px;font:inherit;font-weight:700;cursor:pointer}
button:hover{filter:brightness(1.08)}
button:focus-visible{outline:2px solid #fff;outline-offset:2px}
.err{color:var(--bad);margin:0 0 14px;font-size:14px}
</style></head><body><main>
<div class="mark">STEP<b>WHERE</b></div>
<h1>Sign in</h1>
<p class="sub">Sales Desk · orders from all channels</p>
<form method="post" action="/login">
%ERROR%
<label for="u">Username</label>
<input id="u" name="username" autocomplete="username" autocapitalize="none" required autofocus>
<label for="p">Password</label>
<input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>
</form></main></body></html>"""


def login_page(error=""):
    note = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return LOGIN_HTML.replace("%ERROR%", note)


# ------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "SalesDesk"
    sys_version = ""

    # -- helpers
    def host_ok(self):
        host = self.headers.get("Host", "").lower().split(":")[0]
        allowed = {"localhost", "127.0.0.1"}
        if os.getenv("APP_HOST"):
            allowed.add(os.environ["APP_HOST"].strip().lower())
        return host in allowed

    def origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urllib.parse.urlparse(origin).netloc.lower() == self.headers.get("Host", "").lower()

    def client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For")
        return forwarded.split(",")[0].strip() if forwarded else self.client_address[0]

    def session(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = cookies.SimpleCookie(raw)
        except cookies.CookieError:
            return None
        morsel = jar.get(COOKIE)
        return lookup_session(morsel.value) if morsel else None

    def cookie_header(self, token, max_age):
        parts = [f"{COOKIE}={token}", "HttpOnly", "SameSite=Lax", "Path=/", f"Max-Age={max_age}"]
        if self.headers.get("X-Forwarded-Proto") == "https":
            parts.append("Secure")
        return "; ".join(parts)

    def send(self, body, content_type, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status=200, headers=None):
        self.send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status, headers)

    def redirect(self, where, headers=None):
        self.send(b"", "text/plain", 302, {"Location": where, **(headers or {})})

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length)

    # -- GET
    def do_GET(self):
        if not self.host_ok():
            return self.send_json({"error": "Forbidden"}, 403)
        path = urllib.parse.urlparse(self.path).path
        if path == "/shim.js":
            return self.send(SHIM_FILE.read_bytes(), "application/javascript; charset=utf-8")
        if path == "/login":
            if self.session():
                return self.redirect("/")
            return self.send(login_page().encode("utf-8"), "text/html; charset=utf-8")
        if path in ("/", "/index.html"):
            if not self.session():
                return self.redirect("/login")
            return self.send(render_index().encode("utf-8"), "text/html; charset=utf-8")
        if path.startswith("/api/"):
            sess = self.session()
            if not sess:
                return self.send_json({"error": "Not signed in"}, 401)
            username = sess[0]
            if path == "/api/me":
                return self.send_json({"username": username})
            if path == "/api/status":
                return self.send_json({
                    "shopify": market.credentials_present("shopify"),
                    "amazon": market.credentials_present("amazon"),
                    "last_sync": read_state().get("last_sync"),
                })
            if path == "/api/version":
                version, _data, by = get_state()
                return self.send_json({"version": version, "by": by})
            if path == "/api/state":
                version, data, by = get_state()
                return self.send_json({"version": version, "data": data, "by": by})
        return self.send_json({"error": "Not found"}, 404)

    # -- POST
    def do_POST(self):
        if not self.host_ok() or not self.origin_ok():
            return self.send_json({"error": "Forbidden"}, 403)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            return self.handle_login()
        if parsed.path == "/logout":
            sess = self.session()
            if sess:
                run("DELETE FROM sessions WHERE token_hash=?", (sess[1],), write=True)
                SESSION_CURSORS.pop(sess[1], None)
            return self.send_json({"ok": True}, headers={"Set-Cookie": self.cookie_header("", 0)})
        if parsed.path == "/api/sync":
            return self.handle_sync(parsed)
        return self.send_json({"error": "Not found"}, 404)

    def handle_login(self):
        ip = self.client_ip()
        now = time.time()
        recent = [t for t in LOGIN_FAILS.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        LOGIN_FAILS[ip] = recent
        if len(recent) >= LOGIN_MAX_FAILS:
            return self.send(login_page("Too many attempts. Wait 15 minutes and try again.").encode("utf-8"),
                             "text/html; charset=utf-8", 429)
        body = self.read_body()
        form = urllib.parse.parse_qs((body or b"").decode("utf-8", "replace"))
        username = (form.get("username", [""])[0]).strip().lower()
        password = form.get("password", [""])[0]
        rows = run("SELECT pw FROM users WHERE username=?", (username,)) if valid_username(username) else []
        ok = check_password(password, rows[0]["pw"] if rows else DUMMY_HASH) and bool(rows)
        if not ok:
            recent.append(now)
            return self.send(login_page("Wrong username or password.").encode("utf-8"), "text/html; charset=utf-8", 401)
        LOGIN_FAILS.pop(ip, None)
        token = new_session(username)
        return self.redirect("/", {"Set-Cookie": self.cookie_header(token, SESSION_SECONDS)})

    def handle_sync(self, parsed):
        sess = self.session()
        if not sess:
            return self.send_json({"error": "Not signed in"}, 401)
        channel = urllib.parse.parse_qs(parsed.query).get("channel", ["all"])[0]
        if channel not in {"all", "shopify", "amazon"}:
            return self.send_json({"error": "Unknown channel"}, 400)
        age = last_sync_age()
        # The server already syncs every 2 minutes. A single-channel button always
        # runs; the general call only runs if the last sync is older than a few seconds.
        if channel != "all" or age is None or age > SYNC_THROTTLE_SECONDS:
            try:
                do_sync(channel)
            except Exception as exc:
                kv_set("last_errors", json.dumps([f"Sync failed: {exc}"]))
        cursor = SESSION_CURSORS.setdefault(sess[1], {"acked": 0, "pending": 0})
        rows = run("SELECT seq, data FROM sync_orders WHERE seq>? ORDER BY seq", (cursor["acked"],))
        if rows:
            cursor["pending"] = max(cursor["pending"], rows[-1]["seq"])
        try:
            errors = json.loads(kv_get("last_errors") or "[]")
        except json.JSONDecodeError:
            errors = []
        return self.send_json({
            "orders": [json.loads(r["data"]) for r in rows],
            "errors": errors,
            "last_sync": read_state().get("last_sync"),
        })

    # -- PUT
    def do_PUT(self):
        if not self.host_ok() or not self.origin_ok():
            return self.send_json({"error": "Forbidden"}, 403)
        if urllib.parse.urlparse(self.path).path != "/api/state":
            return self.send_json({"error": "Not found"}, 404)
        sess = self.session()
        if not sess:
            return self.send_json({"error": "Not signed in"}, 401)
        body = self.read_body()
        try:
            payload = json.loads(body or b"")
            base_version, data = payload["base_version"], payload["data"]
            if not isinstance(base_version, int) or not isinstance(data, str):
                raise ValueError
            parsed = json.loads(data)
            if not isinstance(parsed, dict) or ("orders" in parsed and not isinstance(parsed["orders"], list)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self.send_json({"error": "Invalid data"}, 400)
        saved, version = put_state(base_version, data, sess[0])
        if not saved:
            return self.send_json({"error": "conflict", "version": version}, 409)
        cursor = SESSION_CURSORS.get(sess[1])
        if cursor:
            # The browser saved after receiving synced orders, so they need not be sent again.
            cursor["acked"] = max(cursor["acked"], cursor["pending"])
        return self.send_json({"version": version})


# ----------------------------------------------------------------- start-up

def cli(args):
    init_db()
    if args[0] == "users":
        for row in run("SELECT username, created FROM users ORDER BY username"):
            print(f"{row['username']}  (added {row['created'][:10]})")
        return 0
    if len(args) != 2 or args[0] not in {"adduser", "deluser"}:
        print(__doc__)
        return 2
    name = args[1].strip().lower()
    if args[0] == "deluser":
        run("DELETE FROM sessions WHERE username=?", (name,), write=True)
        run("DELETE FROM users WHERE username=?", (name,), write=True)
        print(f"Removed {name} (if they existed).")
        return 0
    if not valid_username(name):
        print("Username: 2-60 characters, using a-z, 0-9 and . _ @ -")
        return 2
    password = getpass.getpass("Password (at least 10 characters): ")
    if len(password) < 10:
        print("Password is too short.")
        return 2
    if password != getpass.getpass("Type it again: "):
        print("The two passwords differ.")
        return 2
    run("INSERT OR REPLACE INTO users(username,pw,created) VALUES(?,?,?)",
        (name, hash_password(password), datetime.now(timezone.utc).isoformat()), write=True)
    run("DELETE FROM sessions WHERE username=?", (name,), write=True)
    print(f"Saved user {name}.")
    return 0


def main():
    env_status = market.load_env()
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    init_db()
    port = int(os.getenv("PORT") or "8080")
    interval = int(os.getenv("SYNC_INTERVAL_SECONDS") or "120")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Stepwhere Sales Desk (hosted) listening on 127.0.0.1:{port}", flush=True)
    print(env_status, flush=True)
    if not run("SELECT 1 FROM users LIMIT 1"):
        print("No users yet. Add one: python3 hosted.py adduser NAME", flush=True)
    if market.shopify_mode():
        threading.Thread(target=market.report_shopify_connection, daemon=True).start()
    else:
        print(market.shopify_setup_hint(), flush=True)
    if interval > 0:
        threading.Thread(target=background_loop, args=(interval,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

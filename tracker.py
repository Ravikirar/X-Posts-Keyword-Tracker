#!/usr/bin/env python3
"""Local, metadata-only tracker for explicit API-credit giveaway posts on X.

The X response necessarily contains post text, but this application never stores,
logs, displays, or exports it. Only post metadata and the canonical X URL are kept.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = Path(os.getenv("TRACKER_DB_PATH", str(DATA_DIR / "tracker.db")))
API_URL = "https://api.x.com/2/tweets/search/recent"
DEFAULT_QUERY = (
    '("API key" OR "API credits" OR "developer credits") '
    '(giveaway OR "have fun" OR "enjoy") -is:retweet -is:reply'
)
QUERY = os.getenv("X_SEARCH_QUERY", DEFAULT_QUERY).strip()
POLL_SECONDS = max(10, int(os.getenv("POLL_SECONDS", "10")))
HOST = os.getenv("TRACKER_HOST", "127.0.0.1")
PORT = int(os.getenv("TRACKER_PORT", "8765"))
USE_SYSTEM_PROXY = os.getenv("TRACKER_USE_SYSTEM_PROXY", "0").strip().lower() in {"1", "true", "yes"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db() -> None:
    with closing(connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                post_id TEXT PRIMARY KEY,
                post_url TEXT NOT NULL,
                author_username TEXT NOT NULL,
                post_created_at TEXT NOT NULL,
                discovered_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.commit()


def get_state(key: str) -> str | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_state(key: str, value: str) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


@dataclass(frozen=True)
class FetchResult:
    saved: int
    newest_id: str | None
    remaining: str | None
    reset_at: str | None


class TrackerError(RuntimeError):
    pass


class RateLimited(TrackerError):
    def __init__(self, retry_at: float):
        super().__init__("X API rate limit reached")
        self.retry_at = retry_at


def _request_json(token: str, since_id: str | None) -> tuple[dict[str, Any], Any]:
    params = {
        "query": QUERY,
        "tweet.fields": "created_at,author_id",
        "expansions": "author_id",
        "user.fields": "username",
        "max_results": "100",
    }
    if since_id:
        params["since_id"] = since_id
    request = urllib.request.Request(
        f"{API_URL}?{urllib.parse.urlencode(params)}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "MetadataOnlyGiveawayTracker/1.0",
            "Accept": "application/json",
        },
    )
    # urllib automatically inherits Windows/environment proxy settings. A stale
    # or restricted proxy commonly causes WinError 10013 even when direct HTTPS
    # connectivity works, so direct connections are the safe default here.
    opener = (
        urllib.request.build_opener()
        if USE_SYSTEM_PROXY
        else urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    try:
        response = opener.open(request, timeout=25)
        with response:
            return json.load(response), response.headers
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            reset = exc.headers.get("x-rate-limit-reset")
            retry_after = exc.headers.get("retry-after")
            retry_at = float(reset) if reset and reset.isdigit() else time.time() + int(retry_after or 60)
            raise RateLimited(retry_at) from None
        # Deliberately do not read or expose the response body.
        raise TrackerError(f"X API returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if getattr(reason, "winerror", None) == 10013:
            raise TrackerError(
                "Direct HTTPS to api.x.com was blocked (WinError 10013). "
                "Run the tracker outside a restricted terminal or allow its Python executable."
            ) from None
        raise TrackerError(f"Could not reach X API: {reason}") from None


def fetch_once(token: str) -> FetchResult:
    since_id = get_state("newest_id")
    payload, headers = _request_json(token, since_id)
    users = {
        user.get("id"): user.get("username", "unknown")
        for user in payload.get("includes", {}).get("users", [])
    }
    discovered_at = utc_now()
    rows: list[tuple[str, str, str, str, str]] = []
    for post in payload.get("data", []):
        post_id = str(post.get("id", ""))
        created_at = str(post.get("created_at", ""))
        username = str(users.get(post.get("author_id"), "unknown"))
        if not post_id or not created_at or username == "unknown":
            continue
        rows.append(
            (
                post_id,
                f"https://x.com/{username}/status/{post_id}",
                username,
                created_at,
                discovered_at,
            )
        )

    saved = 0
    with closing(connect()) as conn:
        for row in rows:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO posts "
                "(post_id, post_url, author_username, post_created_at, discovered_at) "
                "VALUES (?, ?, ?, ?, ?)",
                row,
            )
            saved += cursor.rowcount
        conn.commit()

    newest_id = payload.get("meta", {}).get("newest_id")
    if newest_id:
        set_state("newest_id", str(newest_id))
    return FetchResult(
        saved=saved,
        newest_id=str(newest_id) if newest_id else since_id,
        remaining=headers.get("x-rate-limit-remaining"),
        reset_at=headers.get("x-rate-limit-reset"),
    )


class RuntimeStatus:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.last_check: str | None = None
        self.last_error: str | None = None
        self.last_saved = 0
        self.rate_remaining: str | None = None
        self.next_check_at: str | None = None

    def update(self, **values: Any) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "last_check": self.last_check,
                "last_error": self.last_error,
                "last_saved": self.last_saved,
                "rate_remaining": self.rate_remaining,
                "next_check_at": self.next_check_at,
                "poll_seconds": POLL_SECONDS,
                "metadata_only": True,
            }


STATUS = RuntimeStatus()


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def poll_forever(token: str, stop_event: threading.Event) -> None:
    STATUS.update(running=True)
    delay = 0
    while not stop_event.wait(delay):
        next_delay = POLL_SECONDS
        try:
            result = fetch_once(token)
            STATUS.update(
                last_check=utc_now(),
                last_error=None,
                last_saved=result.saved,
                rate_remaining=result.remaining,
            )
        except RateLimited as exc:
            next_delay = max(POLL_SECONDS, int(exc.retry_at - time.time()) + 2)
            STATUS.update(last_check=utc_now(), last_error="Rate limited; waiting for X to reset.")
        except TrackerError as exc:
            next_delay = max(POLL_SECONDS, 30)
            STATUS.update(last_check=utc_now(), last_error=str(exc))
        STATUS.update(next_check_at=_iso_from_epoch(time.time() + next_delay))
        delay = next_delay
    STATUS.update(running=False, next_check_at=None)


def list_posts(limit: int = 250) -> list[dict[str, str]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT post_id, post_url, author_username, post_created_at, discovered_at "
            "FROM posts ORDER BY post_created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>X Giveaway Link Tracker</title>
<style>
:root{color-scheme:dark;--bg:#090b10;--panel:#121620;--line:#242b3a;--muted:#98a2b3;--ink:#f2f4f7;--accent:#70a5ff;--ok:#58d68d;--bad:#ff7b72}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#15213a 0,transparent 34%),var(--bg);color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,sans-serif}
main{max-width:1120px;margin:auto;padding:44px 24px}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.12em;text-transform:uppercase;font-size:12px}h1{font-size:clamp(30px,5vw,54px);line-height:1.05;margin:10px 0 12px;letter-spacing:-.04em}.sub{color:var(--muted);max-width:720px;font-size:17px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:30px 0}.card,.table-wrap{background:rgba(18,22,32,.92);border:1px solid var(--line);border-radius:16px}.card{padding:16px}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}.value{font-size:18px;font-weight:650;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--ok);margin-right:7px}.dot.bad{background:var(--bad)}.table-wrap{overflow:hidden}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:14px 16px;border-bottom:1px solid var(--line)}th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}tr:last-child td{border:0}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.empty{text-align:center;color:var(--muted);padding:50px!important}.notice{margin-top:14px;color:var(--muted);font-size:13px}.error{color:var(--bad)}
@media(max-width:800px){.cards{grid-template-columns:1fr 1fr}.table-wrap{overflow:auto}table{min-width:740px}}@media(max-width:480px){.cards{grid-template-columns:1fr}}
</style></head><body><main>
<div class="eyebrow">Metadata-only monitor</div><h1>X Giveaway Link Tracker</h1>
<p class="sub">Records links and timestamps for posts matching an explicit giveaway query. Post content and credentials are never stored or shown.</p>
<section class="cards">
<div class="card"><div class="label">Monitor</div><div class="value" id="running">Loading…</div></div>
<div class="card"><div class="label">Last check</div><div class="value" id="lastCheck">—</div></div>
<div class="card"><div class="label">Next check</div><div class="value" id="nextCheck">—</div></div>
<div class="card"><div class="label">Saved last run</div><div class="value" id="saved">0</div></div>
</section>
<div class="table-wrap"><table><thead><tr><th>Post</th><th>Author</th><th>Posted</th><th>Discovered</th></tr></thead><tbody id="rows"><tr><td colspan="4" class="empty">Loading posts…</td></tr></tbody></table></div>
<p class="notice" id="notice">The dashboard refreshes every five seconds.</p>
</main><script>
const fmt=s=>s?new Date(s).toLocaleString():"—";
async function refresh(){try{const [sr,pr]=await Promise.all([fetch('/api/status'),fetch('/api/posts')]);const s=await sr.json(),posts=await pr.json();
document.querySelector('#running').innerHTML=`<span class="dot ${s.running?'':'bad'}"></span>${s.running?'Running':'Stopped'}`;
document.querySelector('#lastCheck').textContent=fmt(s.last_check);document.querySelector('#nextCheck').textContent=fmt(s.next_check_at);document.querySelector('#saved').textContent=s.last_saved;
document.querySelector('#notice').innerHTML=s.last_error?`<span class="error">${esc(s.last_error)}</span>`:`Checks every ${s.poll_seconds} seconds · ${s.rate_remaining??'—'} requests remaining in current X rate window.`;
document.querySelector('#rows').innerHTML=posts.length?posts.map(p=>`<tr><td><a href="${esc(p.post_url)}" target="_blank" rel="noopener noreferrer">Open post ↗</a></td><td>@${esc(p.author_username)}</td><td>${fmt(p.post_created_at)}</td><td>${fmt(p.discovered_at)}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No matching posts recorded yet.</td></tr>';}catch(e){document.querySelector('#notice').innerHTML='<span class="error">Dashboard connection failed.</span>';}}
function esc(v){const d=document.createElement('div');d.textContent=String(v??'');return d.innerHTML}refresh();setInterval(refresh,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode())
        elif path == "/api/status":
            self._send(200, "application/json", json.dumps(STATUS.snapshot()).encode())
        elif path == "/api/posts":
            self._send(200, "application/json", json.dumps(list_posts()).encode())
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid noisy request logs and accidental URL/query disclosure.
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one X search and exit")
    args = parser.parse_args()
    initialize_db()
    token = os.getenv("X_BEARER_TOKEN", "").strip()
    if not token:
        print("X_BEARER_TOKEN is required. See README.md.")
        return 2
    if args.once:
        result = fetch_once(token)
        print(f"Saved {result.saved} new post link(s). No post text was retained.")
        return 0

    stop_event = threading.Event()
    worker = threading.Thread(target=poll_forever, args=(token, stop_event), daemon=True)
    worker.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Dashboard: http://{HOST}:{PORT}")
    print(f"Polling every {POLL_SECONDS}s. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        worker.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

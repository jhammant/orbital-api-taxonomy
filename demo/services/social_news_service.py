#!/usr/bin/env python3
"""Adverse-media / social scan — one company name -> recent news + social mentions.

Aggregates several FREE sources, best-effort (any source can fail without breaking
the response): Google News RSS, Hacker News (Algolia), Reddit search, Bluesky search.
Keyed on CompanyName so Orbital routes it straight onto the company spine.

    python3 demo/services/social_news_service.py     # binds 0.0.0.0:8906
"""
import json
import os
import socketserver
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler

HOST = os.environ.get("SOCIAL_HOST", "0.0.0.0")
PORT = int(os.environ.get("SOCIAL_PORT", "8906"))
UA = "company-brain-demo/1.0 (adverse-media scan; jhammant@gmail.com)"
TIMEOUT = 6


def _get(url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def google_news(name, limit=5):
    q = urllib.parse.quote(f'"{name}"')
    raw = _get(f"https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en")
    root = ET.fromstring(raw)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        src = item.find("source")
        out.append({"title": title,
                    "source": (src.text if src is not None else "Google News"),
                    "channel": "news",
                    "url": (item.findtext("link") or ""),
                    "date": (item.findtext("pubDate") or "")})
        if len(out) >= limit:
            break
    return out


def hacker_news(name, limit=4):
    q = urllib.parse.quote(name)
    d = json.loads(_get(f"https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage={limit}"))
    return [{"title": h.get("title") or "", "source": f"HN ({h.get('points', 0)} pts)",
             "channel": "hackernews", "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
             "date": h.get("created_at") or ""} for h in d.get("hits", []) if h.get("title")]


def reddit(name, limit=4):
    q = urllib.parse.quote(f'"{name}"')
    d = json.loads(_get(f"https://www.reddit.com/search.json?q={q}&limit={limit}&sort=new"))
    out = []
    for c in d.get("data", {}).get("children", []):
        p = c.get("data", {})
        out.append({"title": p.get("title") or "", "source": f"r/{p.get('subreddit', '')}",
                    "channel": "reddit", "url": "https://reddit.com" + (p.get("permalink") or ""),
                    "date": str(p.get("created_utc") or "")})
    return out


def bluesky(name, limit=4):
    q = urllib.parse.quote(f'"{name}"')
    d = json.loads(_get(f"https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={q}&limit={limit}"))
    out = []
    for post in d.get("posts", []):
        txt = (post.get("record", {}) or {}).get("text", "").replace("\n", " ").strip()
        handle = (post.get("author", {}) or {}).get("handle", "")
        out.append({"title": txt[:160], "source": f"@{handle}", "channel": "bluesky",
                    "url": "https://bsky.app", "date": (post.get("record", {}) or {}).get("createdAt", "")})
    return out


def scan(name):
    items, counts = [], {}
    for fn, key in [(google_news, "news"), (hacker_news, "hackernews"),
                    (reddit, "reddit"), (bluesky, "bluesky")]:
        try:
            r = fn(name)
        except Exception:
            r = []
        counts[key] = len(r)
        items.extend(r)
    return {"company": name, "counts": counts, "items": items}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": True})
        if u.path != "/scan":
            return self._send(404, {"error": "not found"})
        q = urllib.parse.parse_qs(u.query)
        name = (q.get("q", [""])[0] or "").strip()[:80]
        if not name:
            return self._send(400, {"error": "q is required"})
        try:
            return self._send(200, scan(name))
        except Exception as e:  # noqa: BLE001
            return self._send(200, {"company": name, "counts": {}, "items": [], "error": str(e)})

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"Social/news scan -> http://{HOST}:{PORT}/scan?q=TESCO%20PLC")
    Server((HOST, PORT), Handler).serve_forever()

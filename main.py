import base64
import re
import time
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

import os
import requests
from flask import Flask, request, jsonify, Response, render_template

# ═══════════════════════════════════════════════════════════════════════════
#  PW Live Proxy — simple, stateless. No login, no MongoDB, no file store,
#  no bot. Every route is driven only by the ?url= you pass in.
# ═══════════════════════════════════════════════════════════════════════════

flask_app = Flask(__name__)

UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pw.live/",
    "Origin": "https://www.pw.live",
    # sec-ch-ua / client-hints — kuch CDN edge nodes bina in headers ke bhi
    # requests ko "non-browser" maan ke drop/slow kar dete hain.
    "sec-ch-ua": '"Chromium";v="126", "Not_A Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

AUTH_PARAMS = {"signature", "policy", "key-pair-id", "expires", "start", "session-id"}
UPSTREAM_TIMEOUT = 15
UPSTREAM_MAX_RETRIES = 2  # transient CDN edge hiccups ke liye


@flask_app.after_request
def add_cors_headers(resp):
    """CORS on every response — success ho ya error."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = "*"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


# ── base64 opaque tokens for the player-mode segment proxy ─────────────────

def _b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64d(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode()


def _inherit_auth_params(seg_url: str, playlist_url: str) -> str:
    """Signed CloudFront playlist ke auth params same-host segments pe copy karo."""
    try:
        seg = urlparse(seg_url)
        pl = urlparse(playlist_url)
        if seg.netloc != pl.netloc:
            return seg_url
        seg_q = dict(parse_qsl(seg.query, keep_blank_values=True))
        seg_lower = {k.lower() for k in seg_q}
        for k, v in parse_qsl(pl.query, keep_blank_values=True):
            if k.lower() in AUTH_PARAMS and k.lower() not in seg_lower:
                seg_q[k] = v
        return urlunparse(seg._replace(query=urlencode(seg_q)))
    except Exception:
        return seg_url


def _fetch_upstream(url: str):
    """
    Upstream fetch with retry + backoff.
      - 2xx aur 4xx dono FINAL maane jaate hain (4xx retry karne se theek
        nahi hoga — e.g. expired signed URL — retry sirf time waste karta
        hai aur player ko zyada der "loading" pe atka deta hai).
      - Sirf 5xx / connection-level errors (timeout, DNS, reset — transient
        CDN edge hiccups) retry hote hain, chhoti backoff ke saath.
    """
    headers = dict(UPSTREAM_HEADERS)
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    last_exc = None
    for attempt in range(UPSTREAM_MAX_RETRIES + 1):
        try:
            r = requests.get(
                url, headers=headers, timeout=UPSTREAM_TIMEOUT, allow_redirects=True
            )
            if r.ok or (400 <= r.status_code < 500):
                return r  # final — 2xx ya 4xx, retry se koi fayda nahi
            last_exc = requests.RequestException(f"Upstream {r.status_code}")
        except requests.RequestException as e:
            last_exc = e
        if attempt < UPSTREAM_MAX_RETRIES:
            time.sleep(0.3 * (attempt + 1))
    raise last_exc


def _rewrite_lines(body: str, playlist_url: str, tok):
    """Shared line-walker — playlist ke har URL line/URI= attr ko tok() se replace karo."""
    out_lines = []
    for line in body.splitlines():
        t = line.strip()
        if not t:
            out_lines.append(line)
            continue
        if t.startswith("#"):
            if "URI=" in t:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: f'URI="{tok(m.group(1))}"',
                    line,
                    flags=re.IGNORECASE,
                )
            out_lines.append(line)
            continue
        out_lines.append(tok(t))
    return "\n".join(out_lines) + "\n"


def _rewrite_for_player(body: str, playlist_url: str) -> str:
    """PLAYER mode — every URL becomes a same-origin proxy token
    (/api/pwlive/seg?u=...). Real CDN URL never reaches the browser."""
    base = request.host_url.rstrip("/")

    def tok(raw: str) -> str:
        absolute = urljoin(playlist_url, raw.strip())
        absolute = _inherit_auth_params(absolute, playlist_url)
        return f"{base}/api/pwlive/seg?u={_b64e(absolute)}"

    return _rewrite_lines(body, playlist_url, tok)


def _rewrite_for_download(body: str, playlist_url: str) -> str:
    """DOWNLOAD mode — every URL becomes the real, absolute CDN URL (with
    the playlist's signed auth params inherited onto segments that need
    them) — NOT proxied through this domain. This is what lets a download
    manager (1DM etc.) pull every segment directly from the CDN in
    parallel, and lets the full playlist just play start-to-end in a
    browser too."""

    def tok(raw: str) -> str:
        absolute = urljoin(playlist_url, raw.strip())
        return _inherit_auth_params(absolute, playlist_url)

    return _rewrite_lines(body, playlist_url, tok)


# ═══════════════════════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════════════════════

ROUTES = {
    "player": "/api/pwlive/player?url=<m3u8-url> — proxied playlist for the web player (same-origin, CORS-safe)",
    "download": "/api/pwlive/download?url=<m3u8-url> — full playlist, real CDN URLs, all segments in parallel (play or hand to a download manager)",
    "seg": "/api/pwlive/seg?u=<token> — internal, used by the player route's rewritten playlist",
    "watch": "/player?url=<m3u8-url> — the actual watchable page",
}


@flask_app.route("/")
def index():
    return jsonify({"status": "ok", "routes": ROUTES})


@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"})


@flask_app.route("/api/pwlive/player")
def api_pwlive_player():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL missing"}), 400

    try:
        r = _fetch_upstream(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    body = _rewrite_for_player(r.text, url)
    return Response(
        body,
        200,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@flask_app.route("/api/pwlive/download")
def api_pwlive_download():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL missing"}), 400

    try:
        r = _fetch_upstream(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    body = _rewrite_for_download(r.text, url)
    return Response(
        body,
        200,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@flask_app.route("/api/pwlive/seg")
def api_pwlive_seg():
    """Internal helper — fetches whatever the opaque token points to. Used
    only by playlists that /api/pwlive/player hands out. Binary segments
    pass straight through; nested playlists get rewritten again."""
    token = request.args.get("u")
    if not token:
        return jsonify({"error": "Missing segment token"}), 400
    try:
        url = _b64d(token)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("bad scheme")
    except Exception:
        return jsonify({"error": "Invalid segment token"}), 400

    try:
        r = _fetch_upstream(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "mpegurl" in ctype or "m3u8" in ctype or parsed.path.lower().endswith(".m3u8"):
        # nested playlist — usko bhi player-mode me rewrite karo
        body = _rewrite_for_player(r.text, url)
        return Response(body, 200, content_type="application/vnd.apple.mpegurl")

    headers = {
        "Cache-Control": "public, max-age=30",
        "Accept-Ranges": "bytes",
    }
    if r.headers.get("Content-Range"):
        headers["Content-Range"] = r.headers["Content-Range"]
    return Response(
        r.content,
        206 if r.status_code == 206 else 200,
        content_type=r.headers.get("Content-Type") or "video/mp2t",
        headers=headers,
    )


@flask_app.route("/player")
def player_page():
    """The watch page — reads ?url= client-side and plays it via
    /api/pwlive/player. Missing url shows an inline error, same as the API."""
    return render_template("player.html")


def run_flask():
    port = int(os.environ.get("PORT", 8000))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    run_flask()

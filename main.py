import base64
import json
import re
import time
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse, unquote

import os
import requests
from requests.adapters import HTTPAdapter, Retry
from flask import Flask, request, jsonify, Response, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

# ═══════════════════════════════════════════════════════════════════════════
#  PW Live Proxy — simple, stateless. No login, no MongoDB, no file store,
#  no bot. Every route is driven only by the ?url= you pass in.
# ═══════════════════════════════════════════════════════════════════════════

flask_app = Flask(__name__)

# Vercel's Python builder specifically looks for a top-level variable named
# `app` (that's the "does not define a top-level 'app' Flask instance"
# error). `flask_app` stays as-is too, so the existing gunicorn command /
# Render Docker deploy are completely unaffected.
app = flask_app

# Render/Vercel TLS ko apne reverse proxy pe terminate karte hain aur andar
# plain HTTP forward karte hain. Isके bina Flask ka request.scheme (jo
# `_rewrite_for_player` ke andar `request.host_url` se absolute
# /api/pwlive/seg links banane me use hota hai) hamesha "http" resolve
# hota — page khud https:// pe load hone ke bawajood. Result: generated
# segment URLs http:// ban jaate the, aur browser un mixed-content
# requests ko silently block kar deta — exactly wahi symptom jo screenshot
# me dikha (native controls aa gaye, thumbnail broken, video 0:00 pe atka
# hua, kabhi play hi nahi hota). ProxyFix Render/Vercel ke ek single
# reverse-proxy hop ke X-Forwarded-Proto/Host/Port headers ko trust karta
# hai taaki request.scheme/host_url sahi "https" resolve ho.
flask_app.wsgi_app = ProxyFix(
    flask_app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1
)

# Headers overridable via env vars (no redeploy needed if PW's CDN starts
# requiring a different Referer/Origin/User-Agent — just set the env var
# on Render/Vercel).
UPSTREAM_HEADERS = {
    "User-Agent": os.environ.get(
        "PWLIVE_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": os.environ.get("PWLIVE_REFERER", "https://www.pw.live/"),
    "Origin": os.environ.get("PWLIVE_ORIGIN", "https://www.pw.live"),
    "Connection": "keep-alive",
    "DNT": "1",
    # sec-ch-ua / sec-fetch-* — kuch CDN edge nodes / WAFs bina in headers
    # ke bhi requests ko "non-browser" maan ke drop/slow/block kar dete hain.
    "sec-ch-ua": '"Chromium";v="126", "Not_A Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}

AUTH_PARAMS = {"signature", "policy", "key-pair-id", "expires", "start", "session-id"}
UPSTREAM_TIMEOUT = 15
UPSTREAM_MAX_RETRIES = 2  # transient CDN edge hiccups ke liye

# Shared session — connection pooling (much faster than a fresh TCP+TLS
# handshake per segment) + an HTTP-level Retry adapter for connection
# resets/timeouts. 4xx (incl. 403) is NEVER retried at this layer — that's
# a definitive CDN decision (expired/invalid signature, blocked, etc.),
# retrying it only wastes time.
_session = requests.Session()
_retry = Retry(
    total=UPSTREAM_MAX_RETRIES,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"],
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=20, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


@flask_app.after_request
def add_cors_headers(resp):
    """CORS on every response — success ho ya error."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = "*"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def _raw_query_param(name: str):
    """Robustly pull ?<name>=... straight from the RAW query string,
    instead of Flask's normal split-on-'&' parsing.

    Why: our routes each take exactly ONE meaningful param — a full CDN
    m3u8/segment URL that has its own query string (Signature, Key-Pair-Id,
    Policy, start, Expires...). If whoever builds the link forgets to
    percent-encode that nested URL, its own '&'-separated params silently
    become SIBLING params of our own endpoint instead of staying part of
    the value — e.g.

        /api/pwlive/player?url=https://cdn/x.m3u8?Signature=A&Key-Pair-Id=B&Policy=C

    parses (by the normal rules) as four separate top-level params, and
    `request.args.get("url")` only returns "...x.m3u8?Signature=A" — the
    rest (crucially Key-Pair-Id) silently vanishes, and the CDN then
    rejects the request with a confusing 403 "MissingKey" error.

    Since none of our routes ever accept any other legitimate query
    param, it's safe to just take everything in the raw query string
    starting right after "<name>=" as the value, verbatim, whether or not
    the caller percent-encoded it. Properly-encoded callers (our own
    player.html included) get back exactly the same value as before.
    """
    qs = request.query_string.decode("utf-8", errors="replace")
    marker = f"{name}="
    if qs.startswith(marker):
        raw_tail = qs[len(marker):]
    else:
        idx = qs.find("&" + marker)
        if idx == -1:
            return None
        raw_tail = qs[idx + 1 + len(marker):]
    return unquote(raw_tail) if raw_tail else None


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


def _cf_b64decode(s: str) -> bytes:
    """CloudFront's URL-safe base64 variant: + -> -, = -> _, / -> ~."""
    s = s.replace("-", "+").replace("_", "=").replace("~", "/")
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    return base64.b64decode(s)


def _check_signed_url_expiry(url: str):
    """CloudFront signed URLs (jaisa PW live/CDN links) carry a base64
    'Policy' param with DateLessThan/DateGreaterThan epoch times. Decode
    it up front so an expired link fails FAST with a clear, actionable
    message instead of a confusing bare 403 from the CDN — saves a round
    trip too. Returns an error string if expired/not-yet-valid, else None."""
    try:
        q = dict(parse_qsl(urlparse(url).query))
        policy_b64 = q.get("Policy")
        if not policy_b64:
            return None
        policy = json.loads(_cf_b64decode(policy_b64))
        cond = policy["Statement"][0]["Condition"]
        now = int(time.time())
        less_than = cond.get("DateLessThan", {}).get("AWS:EpochTime")
        greater_than = cond.get("DateGreaterThan", {}).get("AWS:EpochTime")
        if less_than and now > int(less_than):
            return (
                f"Signed link expired {now - int(less_than)}s ago — "
                "generate a fresh index.m3u8 link."
            )
        if greater_than and now < int(greater_than):
            return "This signed link isn't valid yet (starts in the future)."
    except Exception:
        pass  # policy shape ajeeb ho to bas skip — upstream khud decide karega
    return None


def _extract_upstream_error_detail(r):
    """CloudFront/S3 error responses are a small XML body with a <Message>
    (and <Code>) explaining EXACTLY why — expired/invalid signature,
    missing param, access denied by policy, etc. Surface that instead of
    a bare status code so a 403 is actually actionable, not a guess."""
    try:
        text = (r.text or "")[:2000].strip()
        if not text.startswith("<"):
            return None
        code = re.search(r"<Code>(.*?)</Code>", text, re.IGNORECASE | re.DOTALL)
        message = re.search(r"<Message>(.*?)</Message>", text, re.IGNORECASE | re.DOTALL)
        parts = [m.group(1).strip() for m in (code, message) if m]
        return " — ".join(parts) if parts else None
    except Exception:
        return None


def _error_response(r):
    """Uniform error JSON across all three routes: status + CDN's real
    reason when we can extract one."""
    body = {"error": f"Upstream failed: {r.status_code}"}
    detail = _extract_upstream_error_detail(r)
    if detail:
        body["detail"] = detail
    return jsonify(body), r.status_code


def _fetch_upstream(url: str):
    """
    Upstream fetch with retry + backoff, over a pooled session.
      - 2xx aur 4xx dono FINAL maane jaate hain (4xx retry karne se theek
        nahi hoga — e.g. expired signed URL — retry sirf time waste karta
        hai aur player ko zyada der "loading" pe atka deta hai).
      - Sirf 5xx / connection-level errors (timeout, DNS, reset — transient
        CDN edge hiccups) retry hote hain, chhoti backoff ke saath (session
        ka Retry adapter yeh already handle karta hai; yeh loop upar se ek
        aur application-level safety net hai).
    """
    headers = dict(UPSTREAM_HEADERS)
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    last_exc = None
    for attempt in range(UPSTREAM_MAX_RETRIES + 1):
        try:
            r = _session.get(
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
    url = (_raw_query_param("url") or "").strip()
    if not url:
        return jsonify({"error": "URL missing"}), 400

    expiry_error = _check_signed_url_expiry(url)
    if expiry_error:
        return jsonify({"error": "Link expired", "detail": expiry_error}), 400

    try:
        r = _fetch_upstream(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return _error_response(r)

    body = _rewrite_for_player(r.text, url)
    return Response(
        body,
        200,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@flask_app.route("/api/pwlive/download")
def api_pwlive_download():
    url = (_raw_query_param("url") or "").strip()
    if not url:
        return jsonify({"error": "URL missing"}), 400

    expiry_error = _check_signed_url_expiry(url)
    if expiry_error:
        return jsonify({"error": "Link expired", "detail": expiry_error}), 400

    try:
        r = _fetch_upstream(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return _error_response(r)

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
    token = _raw_query_param("u")
    if not token:
        return jsonify({"error": "Missing segment token"}), 400
    try:
        url = _b64d(token)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("bad scheme")
    except Exception:
        return jsonify({"error": "Invalid segment token"}), 400

    expiry_error = _check_signed_url_expiry(url)
    if expiry_error:
        return jsonify({"error": "Link expired", "detail": expiry_error}), 400

    try:
        r = _fetch_upstream(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return _error_response(r)

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

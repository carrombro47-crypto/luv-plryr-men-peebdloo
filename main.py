import base64
import os
import re
import threading
import time
import unicodedata
import functools
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse, quote

import requests
from flask import (
    Flask, render_template, request, jsonify, redirect, url_for,
    session, Response, send_from_directory,
)

from utils.db import get_db
from utils.text import display_title
from recorder import start_recording, resume_pending

# ─── Configuration ──────────────────────────────────────────────────────────
# Public domain used in every generated link. ONLY line to edit if this
# service's Render domain ever changes.
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://luv-plryr-men-peebdloo.onrender.com"
)

# ─── Server-side Admin Auth (keys never reach the browser) ────────────────
OWNER_NAME = os.environ.get("OWNER_NAME", "ViPvxMS10BRO")
ADMIN_KEYS = ["MS#Admin_R4!xQ8Lp7", "Core$MS_N6v!T2Zk9", "mS@Root_P8#Lm5Qx3"]
VIP_KEYS = ["ToXic#ViPR8m!4QxL7", "tOxic@VipN5v!9ZpK2", "ToXic$ViPX7#rT3Lm8"]

RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# ─── Flask app ──────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
flask_app.secret_key = os.environ.get(
    "SECRET_KEY",
    "c7c8d55d9d8b4a3c2f71b1f5f79c8ea84e8d2c7c3a4b51d70b91ef0fdad5f2f6f13e9a7b8c6d1e24f4a8e9c0b5d3a7f6d8e2c1b9a4f7d5e8c3a6b1d0f9e2c7",
)
flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
flask_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

db = get_db()
lectures_col = db["lectures"]


# ═══════════════════════════════════════════════════════════════════════════
#  HLS PROXY (stream.js logic, ported to Python)
#  - Full CORS on EVERY response (success + error + preflight)
#  - Case-insensitive m3u8 content-type detection
#  - CloudFront signed-URL auth params inherited onto segments
#  - Original URL NEVER reaches the browser (base64 opaque tokens)
# ═══════════════════════════════════════════════════════════════════════════

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


def _rewrite_m3u8(body: str, playlist_url: str, name: str) -> str:
    """Playlist ke saare URLs ko proxy tokens se replace karo."""
    base = request.host_url.rstrip("/")

    def tok(raw: str) -> str:
        absolute = urljoin(playlist_url, raw.strip())
        absolute = _inherit_auth_params(absolute, playlist_url)
        return f"{base}/api/live/{quote(name)}/seg?u={_b64e(absolute)}"

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


def _fetch_upstream(url: str):
    """
    Upstream fetch with retry + backoff — ported from the reference
    stream.js proxy logic:
      - 2xx aur 4xx dono FINAL maane jaate hain (4xx retry karne se theek
        nahi hoga — e.g. expired signed URL — retry sirf time waste karta
        hai aur player ko zyada der "loading" pe atka deta hai).
      - Sirf 5xx / connection-level errors (timeout, DNS, reset — transient
        CDN edge hiccups) retry hote hain, chhoti backoff ke saath.
    Pehle sirf EK attempt tha (koi retry nahi) — isliye ek chhota transient
    upstream glitch turant hi player ko fatal error de deta tha, jo live
    stream ke case me bahut common hai. Ye hi "live nahi chal raha" ke
    symptoms ka ek bada part tha.
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


@flask_app.route("/api/live/<name>/playlist")
def live_playlist(name):
    """Master/media playlist — original URL DB se aati hai, browser kabhi nahi dekhta."""
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return jsonify({"error": "Stream not found"}), 404
    try:
        r = _fetch_upstream(doc["original_url"])
    except requests.RequestException as e:
        return jsonify({"error": f"Upstream error: {e}"}), 502
    if not r.ok:
        return jsonify({"error": f"Upstream failed: {r.status_code}"}), r.status_code

    body = _rewrite_m3u8(r.text, doc["original_url"], name)
    return Response(
        body,
        200,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@flask_app.route("/api/live/<name>/seg")
def live_segment(name):
    """Binary segments / nested playlists — opaque base64 token se fetch."""
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
        # nested playlist — usko bhi rewrite karo
        doc = lectures_col.find_one({"_id": name}, {"original_url": 1})
        playlist_base = doc["original_url"] if doc else url
        body = _rewrite_m3u8(r.text, url, name)
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


# ═══════════════════════════════════════════════════════════════════════════
#  AUTH + ADMIN (Luctyebro jaisa strict login portal — as it is)
# ═══════════════════════════════════════════════════════════════════════════

def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Login required"}), 401
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def _sanitize_name(name: str) -> str:
    """Spaces → hyphens; sirf letters (Hindi/English), numbers, hyphen."""
    name = (name or "").strip()
    name = re.sub(r"\s+", "-", name)
    kept = []
    for ch in name:
        if ch == "-":
            kept.append(ch)
            continue
        if unicodedata.category(ch)[0] in ("L", "N"):
            kept.append(ch)
    slug = "".join(kept)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:100]


@flask_app.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    if (
        data.get("owner_name") == OWNER_NAME
        and data.get("admin_key") in ADMIN_KEYS
        and data.get("vip_key") in VIP_KEYS
    ):
        session.permanent = True
        session["is_admin"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Invalid Name / Admin Key / VIP Key."}), 401


@flask_app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@flask_app.route("/")
def index():
    return render_template("admin.html")


@flask_app.route("/api/generate", methods=["POST"])
@admin_required
def api_generate():
    data = request.get_json(silent=True) or {}
    original_url = (data.get("original_url") or "").strip()
    desired_name = (data.get("name") or "").strip()

    if not original_url:
        return jsonify({"ok": False, "error": "Original m3u8 link required"}), 400
    if not original_url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "Invalid link — valid http(s) URL do"}), 400

    name = _sanitize_name(desired_name)
    if not name:
        return jsonify({
            "ok": False,
            "error": "Invalid class name — sirf letters, numbers aur hyphen(-) allowed hai.",
        }), 400

    now = datetime.utcnow()
    token = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    lectures_col.update_one(
        {"_id": name},
        {
            "$set": {
                "original_url": original_url,
                "status": "LIVE",
                "title": display_title(name),
                "updated_at": now,
            },
            "$setOnInsert": {
                "created_at": now,
                "token": token,
                "duration": None,
                "file_size": None,
                "video_filename": None,
            },
            # Har naye/re-generate hone par watch_gen bump (field na ho to
            # $inc khud 0 se shuru karke 1 kar deta hai) — agar is naam ka
            # koi purana background watcher chal raha ho (purani link ke
            # liye) to wo khud-ba-khud supersede/stop ho jaayega, aur ek
            # naya watcher naye original_url ke liye start hoga neeche.
            "$inc": {"watch_gen": 1},
        },
        upsert=True,
    )
    doc = lectures_col.find_one({"_id": name})

    # Live end hote hi automatic download + local-storage processing ke
    # liye background watcher — koi manual "Start Recording" click zaroori
    # nahi, generate hote hi khud shuru ho jaata hai.
    start_recording(name, original_url, lectures_col)

    public_link = f"{PUBLIC_BASE_URL}/{name}"
    return jsonify({
        "ok": True,
        "name": name,
        "public_link": public_link,
        "status": doc.get("status", "LIVE"),
    })


@flask_app.route("/api/record/<name>", methods=["POST"])
@admin_required
def api_record(name):
    """Manual override/kick — agar kisi wajah se background watcher active
    nahi hai (e.g. race condition) to ise idempotently (re)start karo.
    Normal flow mein iski zaroorat nahi padti — generate hote hi automatic
    watcher already chal raha hota hai."""
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return jsonify({"ok": False, "error": "Stream not found"}), 404
    status = doc.get("status")
    if status == "READY":
        return jsonify({"ok": False, "error": "Already READY"}), 409
    started = start_recording(name, doc["original_url"], lectures_col)
    if not started:
        return jsonify({"ok": True, "status": status, "note": "Watcher already running"})
    return jsonify({"ok": True, "status": doc.get("status", "LIVE")})


@flask_app.route("/api/status/<name>")
def api_status(name):
    """Student page isko poll karta hai — LIVE / PROCESSING / READY."""
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return jsonify({"ok": False, "error": "Not found"}), 404

    status = doc.get("status", "LIVE")
    resp = {"ok": True, "status": status, "title": display_title(name)}

    if status == "READY":
        resp["watch_url"] = f"{PUBLIC_BASE_URL}/recordings/{name}-480p.mp4"
        resp["download_url"] = f"{PUBLIC_BASE_URL}/api/videos/{quote(name)}/download"
        resp["duration"] = doc.get("duration")
        resp["file_size"] = doc.get("file_size")
    elif status == "ERROR":
        resp["error"] = doc.get("error", "Processing failed")
    return jsonify(resp)


@flask_app.route("/recordings/<path:filename>")
def recordings(filename):
    # conditional=True → Range support (Watch Online seek ke liye)
    return send_from_directory(
        RECORDINGS_DIR, filename, conditional=True, mimetype="video/mp4"
    )


@flask_app.route("/api/videos/<name>/download")
def api_download(name):
    """
    Direct browser/device download — Telegram ki koi zaroorat nahi.
    - send_from_directory (Werkzeug send_file) file ko chunks mein
      stream karta hai, poora file kabhi bhi server RAM mein load nahi
      hota — 200-900MB files ke liye bhi safe hai.
    - conditional=True → HTTP Range support (browsers isse resume-able
      / paused-resumed downloads karte hain).
    - as_attachment + download_name → proper
      "Content-Disposition: attachment; filename=..." header, taaki
      click karte hi seedha device storage mein save ho, naye tab mein
      khule nahi.
    """
    doc = lectures_col.find_one({"_id": name}, {"status": 1, "video_filename": 1})
    if not doc:
        return jsonify({"error": "Stream not found"}), 404
    if doc.get("status") != "READY":
        return jsonify({"error": "Video abhi ready nahi hai"}), 409

    filename = doc.get("video_filename") or f"{name}-480p.mp4"
    file_path = os.path.join(RECORDINGS_DIR, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "File missing on server"}), 404

    download_name = f"{display_title(name)}.mp4"
    return send_from_directory(
        RECORDINGS_DIR,
        filename,
        as_attachment=True,
        download_name=download_name,
        mimetype="video/mp4",
        conditional=True,
    )


@flask_app.route("/generated/<name>")
def generated(name):
    doc = lectures_col.find_one({"_id": name}, {"_id": 1, "status": 1})
    if not doc:
        return redirect(url_for("index"))
    public_link = f"{PUBLIC_BASE_URL}/{name}"
    return render_template(
        "generated.html", name=name, public_link=public_link, status=doc.get("status")
    )


@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"})


@flask_app.route("/<name>")
def play(name):
    doc = lectures_col.find_one({"_id": name})
    if not doc:
        return "Link galat hai ya Class expire ho gayi. 😔", 404
    return render_template(
        "player.html",
        name=name,
        title=display_title(name),
        status=doc.get("status", "LIVE"),
    )


# ── Startup recovery ─────────────────────────────────────────────────────
# App start/redeploy hote hi jo lectures LIVE/RECORDING/PROCESSING atki hui
# thi unke background watchers dobara chalu karo, taaki koi bhi live class
# jiska recording pending tha wo aage bhi khud-ba-khud process ho jaaye.
threading.Thread(target=resume_pending, args=(lectures_col,), daemon=True).start()


def run_flask():
    port = int(os.environ.get("PORT", 8000))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    run_flask()

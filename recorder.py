"""
recorder.py — Live → Recorded → 480p → Telegram automatic pipeline.

STATE MACHINE (saved in MongoDB "status" field):

    LIVE
      │  (admin start karta hai — live source ke liye)
      ▼
    RECORDING ──────────────┐
      │ (live end detect hote hi)   │ (ffmpeg khud crash/fail ho jaaye)
      ▼                             ▼
    ENDING                        FAILED
      │ (ffmpeg gracefully stop)
      ▼
    PROCESSING   (480p banna)
      │
      ▼
    UPLOADING    (Telegram MTProto upload)
      │
      ▼
    READY  ──── ya agar upload fail: UPLOAD_FAILED (retry-able, dobara
                 record nahi karni padti, sirf upload retry hoti hai)

FAILED  = recording/processing stage me hard failure (non-retryable
          automatically — admin ko "Start Recording" dobara click karna
          padta hai, jo idempotent hai).

═══════════════════════════════════════════════════════════════════════════
 1) LIVE-END DETECTION (sabse important part)
═══════════════════════════════════════════════════════════════════════════
Sirf ek hi signal pe bharosa nahi karte (koi bhi ek temporary network
failure = false-positive "live end"). Do independent signals combine
karte hain:

  (a) ffmpeg process khud EOF pe exit ho jaaye (server stream close kare) —
      YE SABSE STRONG signal hai (spec ke point 6/point-20.7 ke mutabiq).

  (b) Hamara apna independent playlist-poller (yehi ffmpeg process se alag
      chalta hai) jo:
        - #EXT-X-ENDLIST tag dhoondta hai (strong confirmation — foran
          "ended" maan lete hain)
        - playlist repeatedly unreachable ho ya naye segments na aa rahe ho
          (staleness) — is case me LIVE_END_CONFIRMATION_COUNT consecutive
          baar yehi signal aane ke baad hi "ended" maante hain (taaki ek
          chhota transient CDN glitch false alarm na de)

Class ki typical length 1:30–2:30 hrs hoti hai — is poller ko har waqt
chalate rehna wasteful hai, isliye pehle LIVE_MIN_DURATION_SECONDS (default
90 min) tak sirf ffmpeg ka apna EOF signal dekhte hain (cheap), aur uske
baad LIVE_END_POLL_INTERVAL (default 120s = "har 2 minute") ki cadence pe
active end-checking shuru hoti hai — bilkul jaisa maanga gaya tha.

LIVE_MAX_DURATION_SECONDS (default 150 min) ek hard safety cap hai — agar
detection kisi wajah se trigger na ho paaye, tab bhi is duration ke baad
forcibly recording finalize ho jaati hai (URL expire hone se pehle).

═══════════════════════════════════════════════════════════════════════════
 2) TELEGRAM UPLOAD — MTProto (Pyrogram) via BOT_TOKEN
═══════════════════════════════════════════════════════════════════════════
Plain HTTP Bot API ka upload limit ~50MB hai — lecture videos aam taur pe
200-900MB hoti hain, isliye Pyrogram (MTProto client, bot-mode login) use
karte hain jo ~2GB tak bhej sakta hai, same BOT_TOKEN ke saath.

ENV VARS (Render pe set karo):
  API_ID                — https://my.telegram.org se (MTProto ke liye
                           zaroori hai, Bot API se alag)
  API_HASH               — same jagah se
  BOT_TOKEN               (fallback: TELEGRAM_BOT_TOKEN)
  TELEGRAM_STORAGE_CHAT_ID (fallback: TELEGRAM_CHAT_ID) — jaha upload hoga
  BOT_USERNAME            (fallback: TELEGRAM_BOT_USERNAME) — deep link ke liye

  LIVE_CHECK_INTERVAL=15
  LIVE_INACTIVITY_TIMEOUT=90
  LIVE_END_CONFIRMATION_COUNT=3
  LIVE_END_POLL_INTERVAL=120
  LIVE_MIN_DURATION_SECONDS=5400   (90 min)
  LIVE_MAX_DURATION_SECONDS=9000   (150 min)
  MAX_RETRIES=3
"""

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime

import requests

# ─── Paths ──────────────────────────────────────────────────────────────────
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# ─── Telegram (MTProto) ─────────────────────────────────────────────────────
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_STORAGE_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME") or os.environ.get("TELEGRAM_BOT_USERNAME", "PWSENSEI_FileStoreBot")

# Telegram ka absolute upload ceiling ~2GB hai — safety margin ke saath.
TELEGRAM_ABSOLUTE_MAX_MB = 1950

# ─── Live end-detection (sab configurable via env) ─────────────────────────
LIVE_CHECK_INTERVAL = int(os.environ.get("LIVE_CHECK_INTERVAL", "15"))
LIVE_INACTIVITY_TIMEOUT = int(os.environ.get("LIVE_INACTIVITY_TIMEOUT", "90"))
LIVE_END_CONFIRMATION_COUNT = int(os.environ.get("LIVE_END_CONFIRMATION_COUNT", "3"))
LIVE_END_POLL_INTERVAL = int(os.environ.get("LIVE_END_POLL_INTERVAL", "120"))
LIVE_MIN_DURATION_SECONDS = int(os.environ.get("LIVE_MIN_DURATION_SECONDS", str(90 * 60)))
LIVE_MAX_DURATION_SECONDS = int(os.environ.get("LIVE_MAX_DURATION_SECONDS", str(150 * 60)))

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
MIN_FREE_DISK_GB = float(os.environ.get("MIN_FREE_DISK_GB", "2"))

# ─── Live-CONNECT retry (alag hai live-END-detection se) ───────────────────
# Admin link generate karte hi (class ke shuru hone se PEHLE bhi) recording
# ab automatically shuru ho jaati hai. Lekin agar class abhi shuru nahi
# hui, ffmpeg turant exit ho jaata hai (koi live data nahi milta) — pehle
# isse galti se "live end ho gayi" samajh liya jaata tha aur turant FAILED
# maar diya jaata tha. Ab hum pehle sirf "connection establish" karte hain:
# jab tak actual data record na hone lage, patiently retry karte hain.
LIVE_CONNECT_MAX_WAIT_SECONDS = int(os.environ.get("LIVE_CONNECT_MAX_WAIT_SECONDS", str(30 * 60)))
LIVE_CONNECT_RETRY_DELAY = int(os.environ.get("LIVE_CONNECT_RETRY_DELAY", "15"))
LIVE_CONNECT_ESTABLISH_TIMEOUT = int(os.environ.get("LIVE_CONNECT_ESTABLISH_TIMEOUT", "45"))
LIVE_CONNECT_MIN_BYTES = int(os.environ.get("LIVE_CONNECT_MIN_BYTES", "200000"))  # ~200KB

_active = {}          # name -> threading.Thread
_active_lock = threading.Lock()

# Live playback jaisa hi full browser header set — CDN edge nodes bina in
# headers ke non-browser request maan ke 403 de dete hain.
FFMPEG_HEADERS = (
    "Referer: https://www.pw.live/\r\n"
    "Origin: https://www.pw.live\r\n"
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36\r\n"
)
CHECK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.pw.live/",
    "Origin": "https://www.pw.live",
}


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _log(tag: str, msg: str):
    print(f"[{tag}] {msg}", flush=True)


def _set(col, name, **fields):
    fields.setdefault("updated_at", datetime.utcnow())
    col.update_one({"_id": name}, {"$set": fields})


def _disk_space_ok(min_gb: float = MIN_FREE_DISK_GB) -> bool:
    try:
        free_gb = shutil.disk_usage(RECORDINGS_DIR).free / (1024 ** 3)
        return free_gb >= min_gb
    except Exception:
        return True  # check fail ho jaaye to block mat karo


def _run_ffmpeg(args: list, timeout=None) -> bool:
    """Blocking ffmpeg call — finite operations ke liye (480p encode, VOD download)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
            timeout=timeout,
        )
        return proc.returncode == 0
    except Exception as e:
        _log("FFMPEG", f"error: {e}")
        return False


def _probe_stream_info(path: str):
    """ffprobe se duration + video-stream details nikaalo (validation ke liye)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
        vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        return {
            "duration": float((data.get("format") or {}).get("duration", 0) or 0),
            "height": vstream.get("height") if vstream else None,
            "vcodec": vstream.get("codec_name") if vstream else None,
            "has_video": vstream is not None,
            "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        }
    except Exception as e:
        _log("FFMPEG", f"ffprobe failed for {path}: {e}")
        return None


def _validate_output(path: str):
    """Telegram pe upload karne se PEHLE file ko validate karo (spec section 5)."""
    if not os.path.exists(path):
        return False, "file missing"
    if os.path.getsize(path) == 0:
        return False, "file size 0"
    info = _probe_stream_info(path)
    if not info:
        return False, "ffprobe file ko read nahi kar paaya (corrupt/incomplete?)"
    if not info["has_video"]:
        return False, "video stream nahi mila"
    if not info["duration"] or info["duration"] <= 0:
        return False, "invalid/zero duration"
    return True, info


# ═══════════════════════════════════════════════════════════════════════════
#  1) LIVE-END DETECTOR
# ═══════════════════════════════════════════════════════════════════════════

def _check_playlist_signal(url: str, state: dict) -> str:
    """
    Independent playlist check — ffmpeg se bilkul alag.
    Returns: "alive" | "endlist" | "unreachable"
    `state` dict calls ke beech carry hota hai (staleness track karne ke liye).
    """
    try:
        r = requests.get(url, headers=CHECK_HEADERS, timeout=10)
        if not r.ok:
            return "unreachable"
        text = r.text
        if "#EXT-X-ENDLIST" in text:
            return "endlist"
        segs = tuple(l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#"))
        now = time.time()
        if segs and segs == state.get("last_segs"):
            pass  # koi naya segment nahi aaya
        else:
            state["last_segs"] = segs
            state["last_change_ts"] = now
        return "alive"
    except requests.RequestException:
        return "unreachable"


def _wait_for_live_end(name: str, url: str, proc: subprocess.Popen) -> str:
    """
    Blocking monitor loop — dono signals (ffmpeg EOF + independent playlist
    poll) dekhta hai. Returns reason string jab live end confirmed ho jaaye.
    """
    start = time.time()
    state = {"last_change_ts": start}
    consecutive_bad = 0

    _log("END-DETECTOR", f"{name}: monitoring started "
                          f"(warmup={LIVE_MIN_DURATION_SECONDS}s, "
                          f"poll-after-warmup={LIVE_END_POLL_INTERVAL}s, "
                          f"max={LIVE_MAX_DURATION_SECONDS}s)")

    while True:
        if proc.poll() is not None:
            _log("END-DETECTOR", f"{name}: ffmpeg process khud exit ho gaya (EOF) — recording khatam")
            return "ffmpeg_eof"

        elapsed = time.time() - start
        if elapsed >= LIVE_MAX_DURATION_SECONDS:
            _log("END-DETECTOR", f"{name}: max duration ({LIVE_MAX_DURATION_SECONDS}s) reach ho gaya — force finalize")
            return "max_duration"

        if elapsed < LIVE_MIN_DURATION_SECONDS:
            # Warmup period — sirf ffmpeg ka apna EOF cheaply check karte
            # rehte hain, class itni jaldi khatam hone ki ummeed nahi.
            time.sleep(min(LIVE_CHECK_INTERVAL, max(1, LIVE_MIN_DURATION_SECONDS - elapsed)))
            continue

        # Warmup ke baad — active end-checking, har LIVE_END_POLL_INTERVAL.
        signal = _check_playlist_signal(url, state)
        inactivity = time.time() - state.get("last_change_ts", time.time())

        if signal == "endlist":
            _log("END-DETECTOR", f"{name}: #EXT-X-ENDLIST mila — live confirmed ended")
            return "endlist"

        if signal == "unreachable" or inactivity >= LIVE_INACTIVITY_TIMEOUT:
            consecutive_bad += 1
            reason = "unreachable" if signal == "unreachable" else f"no new segments for {inactivity:.0f}s"
            _log("END-DETECTOR", f"{name}: end-signal {consecutive_bad}/{LIVE_END_CONFIRMATION_COUNT} ({reason})")
            if consecutive_bad >= LIVE_END_CONFIRMATION_COUNT:
                _log("END-DETECTOR", f"{name}: live confirmed ended after {consecutive_bad} consecutive checks")
                return "confirmed_ended"
        else:
            if consecutive_bad:
                _log("END-DETECTOR", f"{name}: playlist phir se alive — end-signal counter reset")
            consecutive_bad = 0

        time.sleep(LIVE_END_POLL_INTERVAL)


def _stop_ffmpeg_gracefully(proc: subprocess.Popen, name: str):
    """'q' bhejo (ffmpeg ka graceful-quit) taaki output file properly finalize ho (moov atom likha jaaye)."""
    if proc.poll() is not None:
        return
    _log("RECORDING", f"{name}: ffmpeg ko gracefully stop kar rahe hain")
    try:
        proc.communicate(input=b"q", timeout=15)
        return
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=10)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _wait_for_connection(raw_path: str, proc: subprocess.Popen,
                          timeout: int = LIVE_CONNECT_ESTABLISH_TIMEOUT,
                          min_bytes: int = LIVE_CONNECT_MIN_BYTES) -> bool:
    """
    Naya start hua ffmpeg process ko thodi der dekhte hain — kya woh actually
    live data likh raha hai? Agar class abhi shuru nahi hui (ya URL/headers
    me koi issue hai), ffmpeg turant EOF/error ho jaata hai bina kuch likhe
    — is case me False return karte hain taaki caller retry kare, "live
    end ho gayi" na samjhe.
    """
    start = time.time()
    while time.time() - start < timeout:
        if proc.poll() is not None:
            return False  # ffmpeg khud hi turant exit ho gaya — connect fail
        try:
            if os.path.exists(raw_path) and os.path.getsize(raw_path) >= min_bytes:
                return True
        except OSError:
            pass
        time.sleep(2)
    try:
        return os.path.exists(raw_path) and os.path.getsize(raw_path) >= min_bytes
    except OSError:
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  2) TELEGRAM UPLOAD (Pyrogram / MTProto)
# ═══════════════════════════════════════════════════════════════════════════

async def _pyrogram_upload_async(path: str, title: str):
    from pyrogram import Client  # deferred import — env me install hona chahiye

    async with Client(
        name="pw_live_uploader",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,   # Render ke ephemeral disk pe session-file persist nahi karni
        no_updates=True,  # sirf upload karna hai, updates receive nahi karne
    ) as app:
        msg = await app.send_video(
            chat_id=CHAT_ID,
            video=path,
            caption=f"🎬 {title}\n\n✅ Quality Education 💎",
            supports_streaming=True,
        )
        file_id = msg.video.file_id if msg.video else None
        return file_id, msg.id


def _upload_to_telegram(path: str, title: str):
    """
    Video Telegram pe MTProto (Pyrogram) se upload karo.
    Returns (file_id, message_id, error_message). error None hai on success.
    Retries transient failures up to MAX_RETRIES times (spec section 18).
    """
    if not (API_ID and API_HASH and BOT_TOKEN and CHAT_ID):
        msg = "API_ID / API_HASH / BOT_TOKEN / TELEGRAM_STORAGE_CHAT_ID env vars missing"
        _log("TELEGRAM", msg)
        return None, None, msg

    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > TELEGRAM_ABSOLUTE_MAX_MB:
        msg = f"File {size_mb:.0f}MB — Telegram ke ~2GB upload limit se bhi bada hai"
        _log("TELEGRAM", msg)
        return None, None, msg  # permanent failure — retry se fayda nahi

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            _log("TELEGRAM", f"upload attempt {attempt + 1}/{MAX_RETRIES + 1} ({size_mb:.1f}MB)")
            file_id, message_id = asyncio.run(_pyrogram_upload_async(path, title))
            if file_id:
                _log("TELEGRAM", f"upload completed ✅ (message_id={message_id})")
                return file_id, message_id, None
            last_err = "Telegram ne file_id nahi diya"
        except Exception as e:
            last_err = str(e)
            _log("TELEGRAM", f"upload attempt {attempt + 1} failed: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(5 * (attempt + 1))
    return None, None, last_err


# ═══════════════════════════════════════════════════════════════════════════
#  3) PROCESSING PIPELINE (shared — LIVE aur VOD dono use karte hain)
# ═══════════════════════════════════════════════════════════════════════════

def _process_and_upload(name: str, col, raw_path: str, out_path: str):
    _set(col, name, status="PROCESSING")
    _log("PROCESSING", f"{name}: 480p banana shuru")

    info = _probe_stream_info(raw_path)
    made_via_copy = False
    if info and info.get("has_video") and info.get("height") and info["height"] <= 480 and info.get("vcodec") == "h264":
        # Already suitable — sirf remux (fast, quality-loss zero), re-encode zaroorat nahi.
        _log("PROCESSING", f"{name}: source already <=480p h264 hai — sirf remux, re-encode skip")
        made_via_copy = _run_ffmpeg(["-i", raw_path, "-c", "copy", "-movflags", "+faststart", out_path])

    if not made_via_copy:
        ok = False
        for attempt in range(MAX_RETRIES + 1):
            ok = _run_ffmpeg([
                "-y", "-i", raw_path,
                "-vf", "scale=-2:480",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart",
                out_path,
            ])
            if ok:
                break
            _log("FFMPEG", f"{name}: 480p encode attempt {attempt + 1} fail — retry")
        if not ok:
            _log("FFMPEG", f"{name}: 480p encode har retry ke baad bhi fail — raw copy fallback")
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
                os.replace(raw_path, out_path)
            except OSError as e:
                _set(col, name, status="FAILED", error=f"480p encode fail + fallback fail: {e}", error_type="ffmpeg_encode")
                _log("ERROR", f"{name}: {e}")
                return

    valid, info_or_err = _validate_output(out_path)
    if not valid:
        _set(col, name, status="FAILED", error=f"Validation failed: {info_or_err}", error_type="validation")
        _log("ERROR", f"{name}: validation failed — {info_or_err}")
        return

    duration = info_or_err["duration"]
    _log("PROCESSING", f"{name}: 480p ready ✅ duration={duration:.0f}s size={os.path.getsize(out_path)/1e6:.1f}MB")

    if os.path.exists(raw_path) and raw_path != out_path:
        try:
            os.remove(raw_path)
        except OSError:
            pass

    _set(col, name, status="UPLOADING", duration=duration)
    _log("UPLOAD", f"{name}: Telegram upload shuru")
    file_id, message_id, err = _upload_to_telegram(out_path, name)

    if file_id:
        _set(
            col, name,
            status="READY",
            telegram_file_id=file_id,
            telegram_message_id=message_id,
            duration=duration,
            file_size=os.path.getsize(out_path),
            upload_error=None,
        )
        _log("READY", f"{name}: ✅ READY (duration={duration:.0f}s)")
    else:
        _set(
            col, name,
            status="UPLOAD_FAILED",
            telegram_file_id=None,
            duration=duration,
            upload_error=err,
        )
        _log("ERROR", f"{name}: ⚠️ Telegram upload fail — {err}")


def _pipeline_live(name: str, original_url: str, col):
    raw_path = os.path.join(RECORDINGS_DIR, f"{name}-raw.mp4")
    out_path = os.path.join(RECORDINGS_DIR, f"{name}-480p.mp4")

    _log("LIVE", f"{name}: recording pipeline shuru — live stream se connect ho raha hai")
    _set(col, name, status="RECORDING")

    ffmpeg_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-headers", FFMPEG_HEADERS,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", original_url,
        "-c", "copy", "-movflags", "+faststart",
        raw_path,
    ]

    # ── PHASE 1: connection establish karo ──────────────────────────────
    # Link generate hote hi recording auto-start ho jaati hai — ho sakta hai
    # class abhi shuru na hui ho, ya ek transient connect-glitch ho. Pehle
    # ek quick ffmpeg-exit ko turant "live end ho gayi" maan liya jaata tha
    # jo galat tha — ab jab tak real data record nahi hone lagta, patiently
    # retry karte hain (max LIVE_CONNECT_MAX_WAIT_SECONDS tak).
    connect_wait_start = time.time()
    attempt = 0
    proc = None
    last_stderr = ""

    while True:
        attempt += 1
        if os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except OSError:
                pass
        try:
            proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            _set(col, name, status="FAILED", error=f"ffmpeg start nahi hua: {e}", error_type="ffmpeg_start")
            _log("ERROR", f"{name}: ffmpeg start fail — {e}")
            return

        _log("LIVE", f"{name}: connect attempt {attempt}…")
        connected = _wait_for_connection(raw_path, proc)

        if connected:
            _log("LIVE", f"{name}: ✅ connected — asli recording shuru")
            break

        try:
            if proc.stderr:
                last_stderr = proc.stderr.read().decode(errors="ignore")[-1500:]
        except Exception:
            pass
        _stop_ffmpeg_gracefully(proc, name)
        try:
            if proc.poll() is None:
                proc.wait(timeout=10)
        except Exception:
            pass

        elapsed_total = time.time() - connect_wait_start
        if elapsed_total >= LIVE_CONNECT_MAX_WAIT_SECONDS:
            _set(
                col, name, status="FAILED",
                error=(
                    f"Live stream se {LIVE_CONNECT_MAX_WAIT_SECONDS // 60} min tak connect nahi ho paya "
                    f"(class abhi shuru nahi hui ho sakti, ya link expire/invalid hai)."
                    + (f" ffmpeg: {last_stderr[-300:]}" if last_stderr else "")
                ),
                error_type="live_connect_timeout",
            )
            _log("ERROR", f"{name}: max connect-wait ({LIVE_CONNECT_MAX_WAIT_SECONDS}s) exceeded, giving up")
            return

        _log("LIVE", f"{name}: abhi live data nahi mil raha — {LIVE_CONNECT_RETRY_DELAY}s me retry "
                      f"(total wait {elapsed_total:.0f}s / {LIVE_CONNECT_MAX_WAIT_SECONDS}s)")
        time.sleep(LIVE_CONNECT_RETRY_DELAY)

    # ── PHASE 2: ab genuinely connected hain — live-end tak monitor karo ──
    reason = _wait_for_live_end(name, original_url, proc)

    _set(col, name, status="ENDING")
    _stop_ffmpeg_gracefully(proc, name)
    try:
        if proc.poll() is None:
            proc.wait(timeout=30)
    except Exception:
        pass

    if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        _set(col, name, status="FAILED", error=f"Recording file khaali/missing (reason={reason})", error_type="recording_empty")
        _log("ERROR", f"{name}: raw recording khaali/missing")
        return

    _log("RECORDING", f"{name}: finalized ({reason}), size={os.path.getsize(raw_path)/1e6:.1f}MB")
    _process_and_upload(name, col, raw_path, out_path)


def _pipeline_vod(name: str, source_url: str, col):
    """
    "Already recorded" mode — admin direct ek recorded/master.m3u8 (VOD)
    playable URL paste karta hai. Live-end-detection ki zaroorat nahi,
    seedha download + process + upload.
    """
    raw_path = os.path.join(RECORDINGS_DIR, f"{name}-raw.mp4")
    out_path = os.path.join(RECORDINGS_DIR, f"{name}-480p.mp4")

    _log("VOD", f"{name}: recorded source download ho raha hai")
    _set(col, name, status="RECORDING")
    ok = _run_ffmpeg([
        "-y", "-headers", FFMPEG_HEADERS,
        "-i", source_url,
        "-c", "copy", "-movflags", "+faststart",
        raw_path,
    ])
    if not ok or not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        _set(col, name, status="FAILED", error="VOD download fail/empty", error_type="vod_download")
        _log("ERROR", f"{name}: VOD download fail")
        return

    _set(col, name, status="ENDING")
    _log("VOD", f"{name}: download complete, size={os.path.getsize(raw_path)/1e6:.1f}MB")
    _process_and_upload(name, col, raw_path, out_path)


def _pipeline(name: str, source_url: str, col, source_type: str = "live"):
    try:
        if not _disk_space_ok():
            _set(col, name, status="FAILED", error="Server pe disk space kam hai", error_type="disk_space")
            _log("ERROR", f"{name}: insufficient disk space, aborting")
            return
        if source_type == "vod":
            _pipeline_vod(name, source_url, col)
        else:
            _pipeline_live(name, source_url, col)
    except Exception as e:
        _log("ERROR", f"{name}: unhandled pipeline exception — {e}")
        _set(col, name, status="FAILED", error=str(e), error_type="unhandled_exception")
    finally:
        with _active_lock:
            _active.pop(name, None)


# ═══════════════════════════════════════════════════════════════════════════
#  Public API (main.py se call hota hai)
# ═══════════════════════════════════════════════════════════════════════════

def start_recording(name: str, source_url: str, col, source_type: str = "live") -> bool:
    """Background thread me pipeline start karo. False agar already chal rahi hai (idempotent guard)."""
    with _active_lock:
        if name in _active:
            return False
        t = threading.Thread(target=_pipeline, args=(name, source_url, col, source_type), daemon=True)
        _active[name] = t
        t.start()
        return True


def _retry_upload_pipeline(name: str, col):
    """480p file already disk pe hai — dobara record kiye bina sirf Telegram upload retry karo."""
    out_path = os.path.join(RECORDINGS_DIR, f"{name}-480p.mp4")
    try:
        if not os.path.exists(out_path):
            _set(col, name, status="UPLOAD_FAILED", upload_error="Recorded file disk pe nahi mila — dobara record karna hoga")
            _log("ERROR", f"{name}: retry-upload — 480p file missing on disk")
            return
        info = _probe_stream_info(out_path)
        duration = info["duration"] if info else None
        _log("UPLOAD", f"{name}: retry upload shuru")
        file_id, message_id, err = _upload_to_telegram(out_path, name)
        if file_id:
            _set(col, name, status="READY", telegram_file_id=file_id, telegram_message_id=message_id,
                 duration=duration, upload_error=None)
            _log("READY", f"{name}: ✅ retry upload succeeded")
        else:
            _set(col, name, status="UPLOAD_FAILED", telegram_file_id=None, duration=duration, upload_error=err)
            _log("ERROR", f"{name}: ⚠️ retry upload failed — {err}")
    finally:
        with _active_lock:
            _active.pop(name, None)


def retry_upload(name: str, col) -> bool:
    """Sirf Telegram upload dobara try karo. False agar already chal rahi hai."""
    with _active_lock:
        if name in _active:
            return False
        t = threading.Thread(target=_retry_upload_pipeline, args=(name, col), daemon=True)
        _active[name] = t
        t.start()
        return True


def resume_pending_jobs(col):
    """
    App start hote hi (ya restart ke baad) call hota hai — spec section 10
    "IDEMPOTENCY / server crash recovery".

    Purani RECORDING/PROCESSING/UPLOADING/ENDING jobs (jo restart ke waqt
    beech me thi) ko dhoondo. Agar 480p file already disk pe maujood hai
    (persistent-disk scenario, status tha UPLOADING) to sirf upload retry
    karo — warna safe FAILED maar do (broken/stuck state hamesha ke liye
    nahi rehni chahiye), taaki admin dobara "Start Recording" kar sake.

    Multiple gunicorn workers ek saath start ho sakte hain — isliye atomic
    find_one_and_update se "claim" karte hain taaki duplicate processing na ho.
    """
    stuck = list(col.find({"status": {"$in": ["RECORDING", "PROCESSING", "UPLOADING", "ENDING"]}}))
    for doc in stuck:
        name = doc["_id"]
        prev_status = doc.get("status")
        claimed = col.find_one_and_update(
            {"_id": name, "status": prev_status},
            {"$set": {"status": "RESUMING", "updated_at": datetime.utcnow()}},
        )
        if not claimed:
            continue  # kisi doosre worker ne already pick kar liya

        out_path = os.path.join(RECORDINGS_DIR, f"{name}-480p.mp4")
        if prev_status == "UPLOADING" and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            _log("MONGODB", f"{name}: stuck UPLOADING job resume ho rahi hai (restart ke baad)")
            retry_upload(name, col)
        else:
            _log("MONGODB", f"{name}: stuck '{prev_status}' job ko FAILED maara ja raha hai (server restart hua tha)")
            _set(
                col, name,
                status="FAILED",
                error="Server restart hua tha is job ke dauraan — 'Start Recording' dobara try karo",
                error_type="server_restart",
            )

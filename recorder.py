"""
recorder.py — Live end hone par automatic pipeline:

  live m3u8
     ↓  ffmpeg (stream copy, jab tak live chale record hota rahe)
  raw recording (original quality)
     ↓  ffmpeg -vf scale=-2:480
  <name>-480p.mp4
     ↓  Telegram Bot API sendVideo (sirf ek baar upload)
  telegram_file_id  → MongoDB (status = READY)

Iske baad File-Store bot /start TOKEN pe saved file_id se turant video
bhej deta hai — dobara upload nahi hota.

ENV VARS (Render pe set karo):
  TELEGRAM_BOT_TOKEN   — file store bot ka token
  TELEGRAM_CHAT_ID     — jis chat/channel me upload karna hai (owner id ya channel id)
  TELEGRAM_BOT_USERNAME— deep link ke liye (default: PWSENSEI_FileStoreBot)
"""

import os
import subprocess
import threading

import requests

RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Telegram Bot API (cloud) ka HARD limit hai ~50MB per upload — isse zyada
# size ki file bhejne pe upload silently/loudly fail ho jaata hai. Isliye
# safety margin ke saath 47MB target rakhte hain.
TELEGRAM_MAX_MB = 47

_active = {}          # name -> threading.Thread
_active_lock = threading.Lock()

# Same header set as main.py's UPSTREAM_HEADERS. Pehle ffmpeg ko sirf
# "Referer" + generic "User-Agent: Mozilla/5.0" milta tha — kaafi CDN edge
# nodes isko non-browser request maan ke 403 de dete the, isliye recording
# silently fail ho jaati thi aur READY status / download link kabhi nahi
# aata tha. Ab woh hi full Chrome-jaisa header set use hota hai jo live
# playback ke liye already kaam kar raha hai.
FFMPEG_HEADERS = (
    "Referer: https://www.pw.live/\r\n"
    "Origin: https://www.pw.live\r\n"
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36\r\n"
)


def _set(col, name, **fields):
    col.update_one({"_id": name}, {"$set": fields})


def _run_ffmpeg(args: list) -> bool:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
            timeout=None,
        )
        return proc.returncode == 0
    except Exception as e:
        print(f"[recorder] ffmpeg error: {e}")
        return False


def _probe_duration(path: str):
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip()) if out.stdout.strip() else None
    except Exception:
        return None


def _encode_target_size(raw_path: str, out_path: str, duration: float, max_mb: int) -> bool:
    """
    Lambi lectures 480p/CRF-28 pe bhi aksar 50MB Telegram limit se bada ban
    jaate hain. Isliye duration se target bitrate calculate karke re-encode
    karte hain taaki final file guaranteed budget ke andar rahe.
    """
    audio_kbps = 96
    # 8% safety headroom container overhead ke liye.
    target_kbps_total = (max_mb * 8192) / max(duration, 1) * 0.92
    video_kbps = max(int(target_kbps_total - audio_kbps), 150)  # 150k floor
    return _run_ffmpeg([
        "-y", "-i", raw_path,
        "-vf", "scale=-2:480",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{int(video_kbps * 1.3)}k",
        "-bufsize", f"{int(video_kbps * 2)}k",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        out_path,
    ])


def _upload_to_telegram(path: str, title: str):
    """
    Video Telegram pe upload karke file_id return karo.
    Returns (file_id, error_message). error_message None hai on success.
    """
    if not BOT_TOKEN or not CHAT_ID:
        msg = "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env var missing on this service"
        print(f"[recorder] {msg} — upload skipped")
        return None, msg

    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > TELEGRAM_MAX_MB + 2:  # compression ke baad bhi thoda over ho sakta hai
        msg = f"File still {size_mb:.1f}MB after compression — Telegram Bot API's ~50MB upload limit se bada hai"
        print(f"[recorder] {msg}")
        return None, msg

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    try:
        with open(path, "rb") as f:
            r = requests.post(
                url,
                data={
                    "chat_id": CHAT_ID,
                    "caption": f"🎬 {title}\n\n✅ Quality Education 💎",
                    "supports_streaming": "true",
                },
                files={"video": f},
                timeout=1800,
            )
        data = r.json()
        if data.get("ok"):
            return data["result"]["video"]["file_id"], None
        msg = data.get("description", "unknown Telegram API error")
        print(f"[recorder] telegram upload failed: {data}")
        return None, msg
    except Exception as e:
        print(f"[recorder] telegram upload error: {e}")
        return None, str(e)


def _pipeline(name: str, original_url: str, col):
    raw_path = os.path.join(RECORDINGS_DIR, f"{name}-raw.mp4")
    out_path = os.path.join(RECORDINGS_DIR, f"{name}-480p.mp4")

    try:
        # ── STEP 1: Live stream record (stream copy — fast, no re-encode) ──
        # ffmpeg live playlist ko tab tak read karta hai jab tak stream end
        # na ho (server EXT-X-ENDLIST bheje ya connection close kare).
        _set(col, name, status="RECORDING")
        ok = _run_ffmpeg([
            "-y",
            "-headers", FFMPEG_HEADERS,
            # Live HLS lambi der chalti hai — beech me ek chhoti network
            # hiccup pehle poori recording ko abort kar deti thi. Ab ffmpeg
            # khud reconnect karke recording jaari rakhega.
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", original_url,
            "-c", "copy",
            "-movflags", "+faststart",
            raw_path,
        ])
        if not ok or not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            _set(col, name, status="ERROR", error="Recording failed/empty")
            return

        # ── STEP 2: 480p version banao (quality-first CRF encode) ──
        _set(col, name, status="PROCESSING")
        ok = _run_ffmpeg([
            "-y", "-i", raw_path,
            "-vf", "scale=-2:480",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            out_path,
        ])
        if not ok:
            # fallback: original recording hi use karo
            os.replace(raw_path, out_path)

        duration = _probe_duration(out_path)

        # ── STEP 2b: Telegram ka ~50MB limit — agar CRF encode se bada bana
        # to duration-based target-bitrate re-encode karke size ke andar
        # fit karo. Lambi lectures (jaise 1hr+) CRF-28 pe aksar 50MB se bahut
        # bada ban jaati hain, jisse upload hamesha silently fail hota tha
        # aur "READY" status hone ke bawajood bhi download kabhi kaam nahi
        # karta tha.
        if os.path.exists(out_path):
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            if size_mb > TELEGRAM_MAX_MB and duration:
                print(f"[recorder] {name}: {size_mb:.1f}MB > {TELEGRAM_MAX_MB}MB limit — re-encoding to target size")
                tmp_path = out_path + ".tmp.mp4"
                if _encode_target_size(raw_path if os.path.exists(raw_path) else out_path, tmp_path, duration, TELEGRAM_MAX_MB):
                    os.replace(tmp_path, out_path)
                    duration = _probe_duration(out_path) or duration
                elif os.path.exists(tmp_path):
                    os.remove(tmp_path)

        # ── STEP 3: Telegram pe ek baar upload → file_id save ──
        file_id, upload_error = _upload_to_telegram(out_path, name)

        if file_id:
            _set(
                col, name,
                status="READY",
                telegram_file_id=file_id,
                duration=duration,
                upload_error=None,
            )
            print(f"[recorder] ✅ {name} READY (duration={duration}, file_id=yes)")
        else:
            # IMPORTANT: pehle status hamesha "READY" ho jaata tha chahe
            # upload fail ho jaaye — isliye download button dikhta tha par
            # kaam kabhi nahi karta tha ("processing" hi bolta rehta tha).
            # Ab "Watch Online" already available hai (video ban chuka hai),
            # sirf Telegram upload jo fail hua uska clear status alag hai —
            # admin isko /api/retry-upload/<name> se dobara try kar sakta hai.
            _set(
                col, name,
                status="UPLOAD_FAILED",
                telegram_file_id=None,
                duration=duration,
                upload_error=upload_error,
            )
            print(f"[recorder] ⚠️ {name} recording/processing done but Telegram upload failed: {upload_error}")

        # raw file cleanup (480p hi keep karte hain)
        if os.path.exists(raw_path) and raw_path != out_path:
            try:
                os.remove(raw_path)
            except OSError:
                pass

    except Exception as e:
        print(f"[recorder] pipeline error for {name}: {e}")
        _set(col, name, status="ERROR", error=str(e))
    finally:
        with _active_lock:
            _active.pop(name, None)


def start_recording(name: str, original_url: str, col) -> bool:
    """Background thread me recording pipeline start karo. False if already running."""
    with _active_lock:
        if name in _active:
            return False
        t = threading.Thread(target=_pipeline, args=(name, original_url, col), daemon=True)
        _active[name] = t
        t.start()
        return True


def _retry_upload_pipeline(name: str, col):
    """480p file already disk pe hai — dobara record kiye bina sirf upload retry karo."""
    out_path = os.path.join(RECORDINGS_DIR, f"{name}-480p.mp4")
    try:
        if not os.path.exists(out_path):
            _set(col, name, status="UPLOAD_FAILED", upload_error="Recorded file disk pe nahi mila — dobara record karna hoga")
            return
        duration = _probe_duration(out_path)
        file_id, upload_error = _upload_to_telegram(out_path, name)
        if file_id:
            _set(col, name, status="READY", telegram_file_id=file_id, duration=duration, upload_error=None)
            print(f"[recorder] ✅ {name} retry upload succeeded")
        else:
            _set(col, name, status="UPLOAD_FAILED", telegram_file_id=None, duration=duration, upload_error=upload_error)
            print(f"[recorder] ⚠️ {name} retry upload failed: {upload_error}")
    finally:
        with _active_lock:
            _active.pop(name, None)


def retry_upload(name: str, col) -> bool:
    """Sirf Telegram upload dobara try karo (recording dobara nahi karni). False if already running."""
    with _active_lock:
        if name in _active:
            return False
        t = threading.Thread(target=_retry_upload_pipeline, args=(name, col), daemon=True)
        _active[name] = t
        t.start()
        return True

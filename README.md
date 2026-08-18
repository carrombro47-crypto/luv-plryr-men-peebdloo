# PW LIVE SYSTEM 🎬🔴

Live class link generator + player + **automatic** post-live download + Telegram delivery — sab ek service mein.

## Flow (Advanced — v2)

```
Admin (strict login portal)
   ↓ original index.m3u8 URL + class name paste karo
MongoDB mein save (original URL kabhi browser/student ko reveal nahi hota)
   ↓ generate hote hi background WATCHER khud start ho jaata hai (koi click zaroori nahi)
Public live link banta hai:  BASE_URL/<class-name>
   ↓ students open karte hain
PW LIVE PLAYER page (custom controls, Go Live button, fullscreen, auto quality)
   ↓ (server-side) watcher har ~20s live playlist poll karta hai
Live khatam (#EXT-X-ENDLIST / link expire) detect hote hi:
   ↓ PRO TRICK: `index.m3u8` → `master.m3u8` (poori class ka full archived VOD playlist)
   ↓ ek-shot ffmpeg download (non-realtime, sliding-window ka issue nahi)
480p version banta hai (ffmpeg)
   ↓
Telegram pe EK BAAR upload (local Bot API server agar configured hai — 2GB tak,
warna remote Bot API — 50MB tak) → file_id MongoDB mein save → status = READY
   ↓
Student page auto-update ho jaata hai:
   [ ▶ Watch Online (speed + skip + seek) ]   [ ⬇ Download Now ]
                              ↓
              https://t.me/<bot>?start=<token>
                              ↓
              File-Store Bot saved file_id se turant video bhejta hai
              Caption: "📝 Titel: <clean title>\n\n📥 Upload By♠: @SmartBoy_ApnaMS"
```

Agar app kabhi restart/redeploy ho jaaye jab koi lecture LIVE/PROCESSING ho, watcher
apne aap resume ho jaata hai (`resume_pending` startup par chalta hai) — kuch bhi
permanently atkta nahi.

## Deploy (Render — Docker)

Dockerfile mein ffmpeg included hai, isliye **Docker runtime** select karo.

⚠️ **Sirf 1 gunicorn worker rakha hai** (Dockerfile mein `--workers 1`) — background
watcher threads process-memory mein rehte hain, 2+ workers hone par video duplicate
download/upload ho jaata (har worker apna watcher start kar deta). Concurrency ke
liye thread count zyada rakha hai (`--threads 32`), worker count nahi.

### Environment Variables

| Var | Kya hai |
|---|---|
| `MONGO_URI` | MongoDB connection string (optional — fallback built-in hai) |
| `MONGO_DB_NAME` | Database name. **Bot ke MONGO_DB_NAME ke SAME hona chahiye** (default: `pw_live_system`) |
| `SECRET_KEY` | Session signing key (Render pe set karo) |
| `PUBLIC_BASE_URL` | Is service ka public domain |
| `OWNER_NAME` | Login portal ka owner name (default: `ViPvxMS10BRO`) |
| `TELEGRAM_BOT_TOKEN` | File-Store bot ka token |
| `TELEGRAM_CHAT_ID` | Jahan recording upload hogi (owner id ya channel id, bot wahan admin/member ho) |
| `TELEGRAM_BOT_USERNAME` | Deep link ke liye (default: `PWSENSEI_FileStoreBot`) |
| `TELEGRAM_LOCAL_API_URL` | (Recommended) Local Telegram Bot API server ka base URL, e.g. `http://<internal-host>:8081`. Not set → remote `api.telegram.org` (50MB limit) use hota hai. Deploy instructions: `telegram-bot-api-server/Dockerfile` (repo root ke bahar, alag folder). |

## Routes

- `/` — strict login portal + link generator
- `/generated/<name>` — generated link + "⚡ FORCE CHECK NOW" (manual watcher kick, optional)
- `/<name>` — student live/recorded player page
- `/api/live/<name>/playlist` — proxied m3u8 (CORS-enabled, original URL hidden)
- `/api/live/<name>/seg?u=<token>` — proxied segments
- `/api/status/<name>` — LIVE / PROCESSING / READY / ERROR (+ clean `title`)
- `/api/record/<name>` — idempotent manual watcher kick (normally not needed)
- `/recordings/<name>-480p.mp4` — Watch Online (Range support, seek/speed/skip)

## Notes

- Live segment 403 (signed URL) problem fixed: playlist ke `Signature`/`Policy`/`Key-Pair-Id`/`start` params segments pe auto-inherit hote hain.
- **`master.m3u8` PRO TRICK**: live sliding-window playlists sirf last few minutes hi rakhte hain. Live end hote hi `master.m3u8` (full session archive) try kiya jaata hai — na mile to bache hue live-window se hi fallback download hota hai.
- Class "name" (slug, jo URL mein use hota hai — spaces se hyphens) hi title ka source hai. Hyphens/underscores hata ke player header aur Telegram caption dono mein clean space-separated title dikhta hai (`utils/text.py::display_title`).
- Telegram Bot API se normal bots max ~50MB upload kar sakte hain. Lambe lectures (480p, ~1.5hr ≈ 300-500MB) ke liye **Telegram Bot API local server** (`telegram-bot-api-server/`) alag Render service ke roop mein deploy karo aur `TELEGRAM_LOCAL_API_URL` set karo — warna sirf Watch Online kaam karega, Download Telegram se fail ho sakta hai. Ye limit Telegram ki hai, code ki nahi.
- Render free tier pe recording RAM/disk limited hai — paid instance recommend.

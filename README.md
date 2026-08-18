# PW LIVE SYSTEM 🎬🔴

Live class link generator + player + auto-recording + Telegram delivery — sab ek service mein.

## Flow

```
Admin (strict login portal)
   ↓ original index.m3u8 URL + class name paste karo
MongoDB mein save (original URL kabhi browser/student ko reveal nahi hota)
   ↓
Public live link banta hai:  BASE_URL/<class-name>
   ↓ students open karte hain
PW LIVE PLAYER page (custom controls, Go Live button, fullscreen, auto quality)
   ↓ "⏺ START RECORDING" dabao (generated page pe)
Background ffmpeg worker live stream record karta hai
   ↓ live end (EXT-X-ENDLIST / stream close)
480p version banta hai (ffmpeg)
   ↓
Telegram pe EK BAAR upload → file_id MongoDB mein save → status = READY
   ↓
Student page auto-update ho jaata hai:
   [ ▶ Watch Online ]   [ ⬇ Download Now ]
                              ↓
              https://t.me/<bot>?start=<token>
                              ↓
              File-Store Bot saved file_id se turant video bhejta hai
```

## Deploy (Render — Docker)

Dockerfile mein ffmpeg included hai, isliye **Docker runtime** select karo.

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

## Routes

- `/` — strict login portal + link generator
- `/generated/<name>` — generated link + START RECORDING button
- `/<name>` — student live player page
- `/api/live/<name>/playlist` — proxied m3u8 (CORS-enabled, original URL hidden)
- `/api/live/<name>/seg?u=<token>` — proxied segments
- `/api/status/<name>` — LIVE / RECORDING / PROCESSING / READY
- `/recordings/<name>-480p.mp4` — Watch Online (Range support)

## Notes

- Live segment 403 (signed URL) problem fixed: playlist ke `Signature`/`Policy`/`Key-Pair-Id`/`start` params segments pe auto-inherit hote hain.
- Telegram Bot API se normal bots max ~50MB upload kar sakte hain. Lambe lectures (480p, ~1.5hr ≈ 300-500MB) ke liye **Telegram Bot API local server** chalana hoga (2GB tak), warna `telegram_file_id` nahi milega aur sirf Watch Online kaam karega. Ye limit Telegram ki hai, code ki nahi.
- Render free tier pe recording RAM/disk limited hai — paid instance recommend.

# PW Live Proxy — simple HLS player + download-playlist API (Flask)

No login, no admin portal, no MongoDB, no file store, no bot connection, no
recording. Fully stateless — every route is driven only by the `?url=`
query param you pass in.

## Repo structure

```
.
├── Dockerfile              # PORT 8000, gunicorn
├── requirements.txt        # Flask, requests, gunicorn — no DB driver
├── main.py                 # all routes
└── templates/
    └── player.html         # /player watch page (default <video controls>, LIVE badge, Go Live btn)
```

## Routes

`ROUTES` dict in `main.py` (also returned as JSON at `GET /`):

| Route | Purpose |
|---|---|
| `GET /api/pwlive/player?url=<m3u8-url>` | Proxied playlist for the web player. Every segment/nested-playlist URL rewritten to a same-origin token (`/api/pwlive/seg?u=...`) — real CDN URL never reaches the browser. |
| `GET /api/pwlive/download?url=<m3u8-url>` | Full playlist rewritten to the real, absolute CDN URLs (signed auth params inherited onto segments that need them). Opening it plays the complete video start-to-end; handing it to a download manager (1DM etc.) pulls all segments directly from the CDN in parallel. Nothing is saved server-side. |
| `GET /api/pwlive/seg?u=<token>` | Internal helper only — used by the rewritten playlist that `/api/pwlive/player` hands out. |
| `GET /player?url=<m3u8-url>` | The actual watch page. |

Both `/api/pwlive/player` and `/api/pwlive/download`:
- Missing `url` → `{"error": "URL missing"}`, status `400`
- Upstream failure → `{"error": "..."}` with the upstream's status (or `502`)
- CORS: `Access-Control-Allow-Origin: *` on every response, success or error

## `/player` page

Plain — a native `<video controls>` element (default seek bar, volume,
fullscreen, everything, browser-default) + hls.js, loading straight from
`/api/pwlive/player`. Two small extras only:
- **LIVE badge** (top-left, red bar + blinking dot) — shown only while the
  loaded playlist is actually live.
- **Go Live button** (top-right, tap-only) — appears only once you're more
  than ~15s behind the live edge; tap jumps you to the live edge. It never
  forces you there — seeking/rewinding works completely normally, and the
  button disappears once you're back at the edge.

## Local dev

```bash
pip install -r requirements.txt
python main.py
# open http://localhost:8000/player?url=<your-index.m3u8-url>
```

## Deploy on Render.com (Docker, free web service)

1. Push this repo to GitHub.
2. Render dashboard → **New** → **Web Service** → connect the repo.
3. Runtime: **Docker** (auto-detects the `Dockerfile`).
4. Nothing else to configure — `PORT=8000` is set in the Dockerfile and
   gunicorn binds to it directly.
5. Deploy. Base URL will be `https://<your-app>.onrender.com`.

Usage once deployed:
- Player page: `https://<your-app>.onrender.com/player?url=<index.m3u8-url>`
- Player API: `https://<your-app>.onrender.com/api/pwlive/player?url=<index.m3u8-url>`
- Download playlist: `https://<your-app>.onrender.com/api/pwlive/download?url=<index.m3u8-url>`

## Env vars

**None required.** No database, no login, no link-generation/storage — the
whole flow is just: take the `?url=` passed in → fetch it → rewrite it →
return it.

## What was removed from the old repo

`recorder.py`, `utils/db.py` (MongoDB), `utils/text.py`, the whole
`static/` folder, `templates/admin.html`, `templates/generated.html` — the
entire login/admin portal, MongoDB-backed link-generator, file store, and
recording/watch-online system are gone. `player.html` was rebuilt from
scratch off the plain reference design (default video controls, simple
LIVE badge) instead of the old custom-control-bar template.

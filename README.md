# PW Live Proxy (Fixed v2.0)

## Kya fix hua
- CORS: har response pe headers + OPTIONS preflight + vercel.json global headers
- Error responses (400/500/403) bhi ab CORS ke saath — browser block nahi karega
- Playlist mein absolute proxy URLs (same-origin alag domain se bhi chalega)
- Upstream timeout (15s) + auto-retry, Range header passthrough
- index.html: professional UI, URL input, quality selector, stats, retry, debug logs

## Deploy
- Vercel: repo push karo — api/stream.js auto serverless banega
- Render/VPS: `npm install && npm start`
- Browser mein kholo → stream URL paste karo → Play

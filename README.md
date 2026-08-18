# PW Live Proxy (Fixed v2.1)

## v2.1 mein kya fix hua (fragLoadError / 403 / blank screen)
- ROOT CAUSE: CloudFront signed URL mein segments (index_6_xxx.ts) mein Signature/Policy/Key-Pair-Id nahi hota
  → CloudFront har segment pe 403 deta tha → hls.js fragLoadError → video nahi chalta tha
- FIX: Proxy ab playlist URL ke auth params ko same-host segments pe auto-inherit karta hai
- Content-Type case-insensitive check (application/x-mpegURL bhi match hota hai)
- CORS: har response pe headers + OPTIONS preflight + vercel.json global headers
- Absolute proxy URLs (page kisi bhi domain se kholo)
- Upstream timeout (15s) + retry, Range passthrough

## Deploy
- Vercel: repo push karo — api/stream.js auto serverless banega
- Render/VPS: npm install && npm start
- IMPORTANT: Render pe purana deploy replace karo (purge/redeploy), warna purana code hi chalega

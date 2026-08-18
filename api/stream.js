// ============================================================
//  PW Live Proxy — Vercel Serverless Function (api/stream.js)
//  v2.1 — fragLoadError / CORS / 403 fixes:
//  - Content-Type case-insensitive m3u8 detection
//  - Signed URL auth params (Signature/Policy/Key-Pair-Id)
//    playlist se segments pe auto-inherit hote hain
//  - Full CORS (preflight + success + errors)
//  - Absolute proxy URLs, timeout + retry, Range passthrough
// ============================================================

const UPSTREAM_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
  "Accept": "*/*",
  "Accept-Language": "en-US,en;q=0.9",
  "Referer": "https://www.pw.live/",
  "Origin": "https://www.pw.live",
  "sec-ch-ua": '"Chromium";v="126", "Not_A Brand";v="8"',
  "sec-ch-ua-mobile": "?0",
  "sec-ch-ua-platform": '"Windows"',
};

const TIMEOUT_MS = 15000;
const MAX_RETRIES = 2;

// CloudFront signed URL ke ye params har request pe chahiye hote hain
const AUTH_PARAMS = [
  "signature",
  "policy",
  "key-pair-id",
  "expires",
  "start",
  "session-id",
];

// ---- CORS: har response (success/error) pe lagao ----
function applyCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "*");
  res.setHeader("Access-Control-Expose-Headers", "*");
  res.setHeader("Access-Control-Max-Age", "86400");
}

// ---- Absolute proxy base URL (host se auto-detect) ----
function proxyBase(req) {
  const proto = req.headers["x-forwarded-proto"] || "https";
  const host = req.headers["x-forwarded-host"] || req.headers.host;
  return `${proto}://${host}`;
}

// ---- Timeout ke saath fetch ----
async function fetchWithTimeout(url, options = {}, timeoutMs = TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

// ---- Retry wrapper ----
async function fetchUpstream(url, extraHeaders = {}) {
  let lastError = null;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const response = await fetchWithTimeout(url, {
        headers: { ...UPSTREAM_HEADERS, ...extraHeaders },
        redirect: "follow",
      });
      if (response.ok || (response.status >= 400 && response.status < 500)) {
        return response; // 4xx final hai
      }
      lastError = new Error(`Upstream ${response.status}`);
    } catch (err) {
      lastError = err;
    }
    await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
  }
  throw lastError || new Error("Upstream fetch failed");
}

// ---- Segment URL mein auth params inherit karo ----
// Playlist URL mein Signature/Policy/Key-Pair-Id hote hain,
// par relative segment URLs mein nahi → CloudFront 403 deta hai.
function inheritAuthParams(absoluteUrl, playlistUrl) {
  try {
    const seg = new URL(absoluteUrl);
    const pl = new URL(playlistUrl);

    // Sirf same host pe inherit karo (security + zaroorat)
    if (seg.host !== pl.host) return absoluteUrl;

    let changed = false;
    // URLSearchParams case-sensitive hai — lowercase compare karo
    const segKeysLower = new Set(
      [...seg.searchParams.keys()].map((k) => k.toLowerCase())
    );
    for (const [key, val] of pl.searchParams.entries()) {
      const lower = key.toLowerCase();
      if (AUTH_PARAMS.includes(lower) && !segKeysLower.has(lower)) {
        seg.searchParams.set(key, val);
        changed = true;
      }
    }
    return changed ? seg.href : absoluteUrl;
  } catch {
    return absoluteUrl;
  }
}

export default async function handler(req, res) {
  // CORS sabse pehle — taaki error responses bhi block na hon
  applyCors(res);

  // Preflight request
  if (req.method === "OPTIONS") {
    return res.status(204).end();
  }

  if (req.method !== "GET" && req.method !== "HEAD") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const targetUrl = req.query.url;

  if (!targetUrl) {
    return res.status(400).json({ error: "Missing url parameter" });
  }

  let parsed;
  try {
    parsed = new URL(targetUrl);
    if (!["http:", "https:"].includes(parsed.protocol)) {
      throw new Error("Bad protocol");
    }
  } catch {
    return res.status(400).json({ error: "Invalid url parameter" });
  }

  try {
    // Range header passthrough
    const extraHeaders = {};
    if (req.headers.range) extraHeaders["Range"] = req.headers.range;

    const response = await fetchUpstream(targetUrl, extraHeaders);

    if (!response.ok) {
      return res
        .status(response.status)
        .json({ error: `Upstream failed: ${response.status}` });
    }

    // ---- CASE-INSENSITIVE content-type check (fix #1) ----
    const contentType = (response.headers.get("content-type") || "").toLowerCase();

    const isM3U8 =
      contentType.includes("mpegurl") ||
      contentType.includes("m3u8") ||
      parsed.pathname.toLowerCase().endsWith(".m3u8");

    // ---------- Binary segments (.ts / .m4s / .mp4 / keys) ----------
    if (!isM3U8) {
      const buffer = Buffer.from(await response.arrayBuffer());
      res.setHeader("Content-Type", contentType || "video/mp2t");
      res.setHeader("Content-Length", buffer.length);
      res.setHeader("Cache-Control", "public, max-age=30");
      const cr = response.headers.get("content-range");
      if (cr) res.setHeader("Content-Range", cr);
      res.setHeader("Accept-Ranges", "bytes");
      return res.status(response.status === 206 ? 206 : 200).send(buffer);
    }

    // ---------- M3U8 playlist: parse + rewrite ----------
    let body = await response.text();
    const baseUrl = new URL(targetUrl);
    const base = proxyBase(req);

    const makeProxyUrl = (rawUrl) => {
      try {
        let absolute = /^https?:\/\//i.test(rawUrl)
          ? rawUrl
          : new URL(rawUrl, baseUrl).href;

        // ---- fix #2: signed URL auth params inherit ----
        absolute = inheritAuthParams(absolute, targetUrl);

        // ABSOLUTE proxy URL — kahin se bhi page kholo, kaam karega
        return `${base}/api/stream?url=${encodeURIComponent(absolute)}`;
      } catch {
        return rawUrl;
      }
    };

    const lines = body.split(/\r?\n/);
    const newLines = lines.map((line) => {
      const trimmed = line.trim();
      if (!trimmed) return line;

      if (trimmed.startsWith("#")) {
        // EXT-X-KEY, EXT-X-MAP, EXT-X-MEDIA, EXT-X-I-FRAME-STREAM-INF etc.
        if (trimmed.includes("URI=")) {
          return trimmed.replace(/URI="([^"]+)"/gi, (_, uri) => {
            return `URI="${makeProxyUrl(uri)}"`;
          });
        }
        return line;
      }

      // Segment / nested playlist URL line
      return makeProxyUrl(trimmed);
    });

    body = newLines.join("\n");

    res.setHeader("Content-Type", "application/vnd.apple.mpegurl");
    res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
    return res.status(200).send(body);
  } catch (err) {
    console.error("Proxy Error:", err);
    const isTimeout = err && err.name === "AbortError";
    return res.status(isTimeout ? 504 : 502).json({
      error: isTimeout
        ? "Upstream timeout — stream server slow hai, retry karo"
        : `Proxy error: ${err.message}`,
    });
  }
}

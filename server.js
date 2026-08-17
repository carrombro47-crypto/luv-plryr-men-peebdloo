// ============================================================
//  PW Live Proxy — Express Server (Render / VPS / local)
//  - Global CORS middleware (preflight + success + errors)
//  - Absolute proxy URLs in rewritten playlists
//  - Upstream timeout + retry, Range passthrough
//  - index.html static serve
// ============================================================

import express from "express";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const PORT = process.env.PORT || 3000;

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

// ---------- GLOBAL CORS MIDDLEWARE (sabse pehle) ----------
// Har response pe — success ho ya error — CORS headers jaayenge.
app.use((req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "*");
  res.setHeader("Access-Control-Expose-Headers", "*");
  res.setHeader("Access-Control-Max-Age", "86400");
  if (req.method === "OPTIONS") {
    return res.status(204).end(); // preflight yahin handle
  }
  next();
});

// ---------- Static files (index.html) ----------
app.use(express.static(__dirname));

app.get("/", (req, res) => {
  res.sendFile(path.join(__dirname, "index.html"));
});

app.get("/health", (req, res) => {
  res.json({ status: "ok", uptime: process.uptime() });
});

// ---------- Helpers ----------
async function fetchWithTimeout(url, options = {}, timeoutMs = TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function fetchUpstream(url, extraHeaders = {}) {
  let lastError = null;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const response = await fetchWithTimeout(url, {
        headers: { ...UPSTREAM_HEADERS, ...extraHeaders },
        redirect: "follow",
      });
      if (response.ok || (response.status >= 400 && response.status < 500)) {
        return response;
      }
      lastError = new Error(`Upstream ${response.status}`);
    } catch (err) {
      lastError = err;
    }
    await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
  }
  throw lastError || new Error("Upstream fetch failed");
}

function proxyBase(req) {
  const proto = req.headers["x-forwarded-proto"] || req.protocol || "http";
  const host = req.headers["x-forwarded-host"] || req.headers.host;
  return `${proto}://${host}`;
}

// ---------- Main proxy route ----------
app.get("/api/stream", async (req, res) => {
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
    const extraHeaders = {};
    if (req.headers.range) extraHeaders["Range"] = req.headers.range;

    const response = await fetchUpstream(targetUrl, extraHeaders);

    if (!response.ok) {
      return res
        .status(response.status)
        .json({ error: `Upstream failed: ${response.status}` });
    }

    const contentType = response.headers.get("content-type") || "";

    const isM3U8 =
      contentType.includes("mpegurl") ||
      contentType.includes("m3u8") ||
      parsed.pathname.toLowerCase().endsWith(".m3u8");

    // ---------- M3U8 playlist ----------
    if (isM3U8) {
      let body = await response.text();
      const baseUrl = new URL(targetUrl);
      const base = proxyBase(req);

      const makeProxyUrl = (rawUrl) => {
        try {
          const absolute = /^https?:\/\//i.test(rawUrl)
            ? rawUrl
            : new URL(rawUrl, baseUrl).href;
          return `${base}/api/stream?url=${encodeURIComponent(absolute)}`;
        } catch {
          return rawUrl;
        }
      };

      body = body
        .split(/\r?\n/)
        .map((line) => {
          const trimmed = line.trim();
          if (!trimmed) return line;

          if (trimmed.startsWith("#")) {
            if (trimmed.includes("URI=")) {
              return trimmed.replace(/URI="([^"]+)"/gi, (_, uri) => {
                return `URI="${makeProxyUrl(uri)}"`;
              });
            }
            return line;
          }
          return makeProxyUrl(trimmed);
        })
        .join("\n");

      res.setHeader("Content-Type", "application/vnd.apple.mpegurl");
      res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
      return res.status(200).send(body);
    }

    // ---------- Binary media ----------
    const buffer = Buffer.from(await response.arrayBuffer());
    res.setHeader("Content-Type", contentType || "application/octet-stream");
    res.setHeader("Content-Length", buffer.length);
    res.setHeader("Cache-Control", "public, max-age=30");
    const cr = response.headers.get("content-range");
    if (cr) res.setHeader("Content-Range", cr);
    res.setHeader("Accept-Ranges", "bytes");
    return res.status(response.status === 206 ? 206 : 200).send(buffer);
  } catch (error) {
    console.error("Proxy error:", error);
    const isTimeout = error && error.name === "AbortError";
    return res.status(isTimeout ? 504 : 502).json({
      error: isTimeout
        ? "Upstream timeout — retry karo"
        : `Proxy error: ${error.message}`,
    });
  }
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`✅ PW Live Proxy running on port ${PORT}`);
});

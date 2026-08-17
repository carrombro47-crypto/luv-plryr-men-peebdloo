export default async function handler(req, res) {
  // Strong CORS
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "*");
  res.setHeader("Access-Control-Expose-Headers", "*");

  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  const targetUrl = req.query.url;
  if (!targetUrl) {
    return res.status(400).send("Missing url parameter");
  }

  try {
    const response = await fetch(targetUrl, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pw.live/",
        "Origin": "https://www.pw.live",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"'
      },
      redirect: "follow"
    });

    if (!response.ok) {
      return res.status(response.status).send(`Upstream failed: ${response.status}`);
    }

    const contentType = response.headers.get("content-type") || "";
    const isM3U8 =
      contentType.includes("mpegurl") ||
      contentType.includes("m3u8") ||
      targetUrl.includes(".m3u8");

    // Binary segments (.ts / .m4s / .mp4 / keys)
    if (!isM3U8) {
      const buffer = await response.arrayBuffer();
      res.setHeader("Content-Type", contentType || "video/mp2t");
      res.setHeader("Cache-Control", "public, max-age=5");
      return res.status(200).send(Buffer.from(buffer));
    }

    // ---------- m3u8 Parsing & Rewriting ----------
    let body = await response.text();
    const baseUrl = new URL(targetUrl);

    const makeProxyUrl = (rawUrl) => {
      try {
        let absolute;
        if (rawUrl.startsWith("http://") || rawUrl.startsWith("https://")) {
          absolute = rawUrl;
        } else {
          absolute = new URL(rawUrl, baseUrl).href;
        }
        // Proxy ke through bhejo
        return `/api/stream?url=${encodeURIComponent(absolute)}`;
      } catch (e) {
        return rawUrl;
      }
    };

    const lines = body.split(/\r?\n/);
    const newLines = lines.map((line) => {
      const trimmed = line.trim();

      // Empty line
      if (!trimmed) return line;

      // Comment / Tag lines
      if (trimmed.startsWith("#")) {
        // EXT-X-KEY, EXT-X-MAP, EXT-X-MEDIA, EXT-X-I-FRAME-STREAM-INF etc.
        if (trimmed.includes("URI=")) {
          return trimmed.replace(/URI="([^"]+)"/gi, (_, uri) => {
            return `URI="${makeProxyUrl(uri)}"`;
          });
        }
        return line;
      }

      // Segment / playlist URL line
      return makeProxyUrl(trimmed);
    });

    body = newLines.join("\n");

    res.setHeader("Content-Type", "application/vnd.apple.mpegurl");
    res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
    return res.status(200).send(body);

  } catch (err) {
    console.error("Proxy Error:", err);
    return res.status(500).send("Proxy error: " + err.message);
  }
}

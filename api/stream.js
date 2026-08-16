export default async function handler(req, res) {
  // CORS
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
      console.error("Upstream error:", response.status, response.statusText);
      return res.status(response.status).send(`Upstream failed: ${response.status}`);
    }

    const contentType = response.headers.get("content-type") || "";
    const isM3U8 = contentType.includes("mpegurl") || 
                   contentType.includes("m3u8") || 
                   targetUrl.includes(".m3u8");

    // Binary segments (.ts, .m4s, .mp4) ko directly forward karo
    if (!isM3U8) {
      const buffer = await response.arrayBuffer();
      res.setHeader("Content-Type", contentType || "video/mp2t");
      res.setHeader("Cache-Control", "public, max-age=10");
      return res.status(200).send(Buffer.from(buffer));
    }

    // m3u8 text handle karo
    let body = await response.text();
    const baseUrl = new URL(targetUrl);

    // Helper: koi bhi URL ko proxy ke through banao
    const makeProxyUrl = (rawUrl) => {
      try {
        let absolute;
        if (rawUrl.startsWith("http://") || rawUrl.startsWith("https://")) {
          absolute = rawUrl;
        } else {
          absolute = new URL(rawUrl, baseUrl).href;
        }
        return `/api/stream?url=${encodeURIComponent(absolute)}`;
      } catch (e) {
        return rawUrl;
      }
    };

    // Lines process karo
    const lines = body.split("\n");
    const newLines = lines.map(line => {
      const trimmed = line.trim();

      // Empty or comment
      if (!trimmed || trimmed.startsWith("#")) {
        // EXT-X-KEY URI rewrite
        if (trimmed.includes("URI=")) {
          return trimmed.replace(/URI="([^"]+)"/, (_, uri) => {
            return `URI="${makeProxyUrl(uri)}"`;
          });
        }
        // EXT-X-MAP URI rewrite
        if (trimmed.includes("URI=")) {
          return trimmed.replace(/URI="([^"]+)"/, (_, uri) => {
            return `URI="${makeProxyUrl(uri)}"`;
          });
        }
        return line;
      }

      // Segment line (not starting with #)
      return makeProxyUrl(trimmed);
    });

    body = newLines.join("\n");

    res.setHeader("Content-Type", "application/vnd.apple.mpegurl");
    res.setHeader("Cache-Control", "no-cache, no-store");
    return res.status(200).send(body);

  } catch (err) {
    console.error("Proxy Error:", err);
    return res.status(500).send("Proxy error: " + err.message);
  }
}

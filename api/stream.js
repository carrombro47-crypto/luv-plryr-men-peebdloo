export default async function handler(req, res) {
  // CORS allow karo
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "*");

  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  const targetUrl = req.query.url;

  if (!targetUrl) {
    return res.status(400).json({ error: "url parameter required" });
  }

  try {
    const response = await fetch(targetUrl, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.pw.live/",
        "Origin": "https://www.pw.live"
      }
    });

    if (!response.ok) {
      return res.status(response.status).send("Failed to fetch stream");
    }

    const contentType = response.headers.get("content-type") || "";
    let body = await response.text();

    // Agar m3u8 file hai to segment links rewrite karo
    if (contentType.includes("mpegurl") || targetUrl.includes(".m3u8")) {
      const baseUrl = new URL(targetUrl);
      
      // Relative links ko absolute + proxy ke through banao
      body = body.replace(/(.*\.ts.*)/g, (match) => {
        // Agar already full URL hai
        if (match.startsWith("http")) {
          return `/api/stream?url=${encodeURIComponent(match)}`;
        }
        // Relative path hai
        const fullSegmentUrl = new URL(match, baseUrl).href;
        return `/api/stream?url=${encodeURIComponent(fullSegmentUrl)}`;
      });

      // EXT-X-KEY (agar encryption ho) bhi rewrite kar sakte ho
      body = body.replace(/(URI=")([^"]+)(")/g, (match, p1, uri, p3) => {
        if (uri.startsWith("http")) {
          return `\( {p1}/api/stream?url= \){encodeURIComponent(uri)}${p3}`;
        }
        const fullKeyUrl = new URL(uri, baseUrl).href;
        return `\( {p1}/api/stream?url= \){encodeURIComponent(fullKeyUrl)}${p3}`;
      });
    }

    res.setHeader("Content-Type", contentType || "application/vnd.apple.mpegurl");
    res.setHeader("Cache-Control", "no-cache");
    return res.status(200).send(body);

  } catch (err) {
    console.error(err);
    return res.status(500).json({ error: "Proxy error", message: err.message });
  }
}

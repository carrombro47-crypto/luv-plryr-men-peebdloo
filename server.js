import express from "express";

const app = express();
const PORT = process.env.PORT || 3000;

app.get("/", (req, res) => {
  res.send("PW Live Proxy is running ✅");
});

app.get("/health", (req, res) => {
  res.json({ status: "ok" });
});

app.get("/api/stream", async (req, res) => {
  const targetUrl = req.query.url;

  if (!targetUrl) {
    return res.status(400).send("Missing url parameter");
  }

  try {
    const response = await fetch(targetUrl);

    if (!response.ok) {
      return res
        .status(response.status)
        .send(`Upstream failed: ${response.status}`);
    }

    const contentType = response.headers.get("content-type") || "";

    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "*");

    const isM3U8 =
      contentType.includes("mpegurl") ||
      contentType.includes("m3u8") ||
      targetUrl.includes(".m3u8");

    // M3U8 playlist
    if (isM3U8) {
      let body = await response.text();
      const baseUrl = new URL(targetUrl);

      const makeProxyUrl = (rawUrl) => {
        try {
          const absoluteUrl = new URL(rawUrl, baseUrl).href;

          return `/api/stream?url=${encodeURIComponent(absoluteUrl)}`;
        } catch {
          return rawUrl;
        }
      };

      body = body
        .split("\n")
        .map((line) => {
          const trimmed = line.trim();

          if (!trimmed) return line;

          // Rewrite URI="..."
          if (trimmed.startsWith("#") && trimmed.includes("URI=")) {
            return line.replace(/URI="([^"]+)"/, (_, uri) => {
              return `URI="${makeProxyUrl(uri)}"`;
            });
          }

          // Rewrite media segments
          if (!trimmed.startsWith("#")) {
            return makeProxyUrl(trimmed);
          }

          return line;
        })
        .join("\n");

      res.setHeader(
        "Content-Type",
        "application/vnd.apple.mpegurl"
      );

      return res.status(200).send(body);
    }

    // Binary media
    const buffer = Buffer.from(await response.arrayBuffer());

    res.setHeader(
      "Content-Type",
      contentType || "application/octet-stream"
    );

    return res.status(200).send(buffer);

  } catch (error) {
    console.error("Proxy error:", error);
    return res.status(500).send("Proxy error");
  }
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Server running on port ${PORT}`);
});

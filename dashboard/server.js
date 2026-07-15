// Porch Lockbox dashboard server - zero dependencies, runs on the Mac.
//
//   node dashboard/server.js          (from the repo root)
//
// The camera phone mirrors its cloud-bound frames and event photos here;
// this serves the household dashboard on the LAN and proxies lock commands
// to the ESP32 (the Mac is on the home network, so the LAN rule holds).

const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = 8321;
const DIR = __dirname;
const EVENTS_FILE = path.join(DIR, "events.json");
const IMAGES_DIR = path.join(DIR, "event-images");
fs.mkdirSync(IMAGES_DIR, { recursive: true });

// ESP32 host comes from the repo's .env
function esp32Host() {
  try {
    const env = fs.readFileSync(path.join(DIR, "..", ".env"), "utf8");
    const m = env.match(/^ESP32_IP=(.+)$/m);
    return m ? m[1].trim() : null;
  } catch {
    return null;
  }
}

let latestSnapshot = null; // Buffer
let latestSnapshotAt = 0;

function loadEvents() {
  try {
    return JSON.parse(fs.readFileSync(EVENTS_FILE, "utf8"));
  } catch {
    return [];
  }
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
  });
}

function proxyLock(pathname, res) {
  const host = esp32Host();
  if (!host) {
    res.writeHead(500, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: false, error: "ESP32_IP missing from .env" }));
  }
  const req = http.get({ host: host.split(":")[0], port: 80, path: pathname, timeout: 5000 }, (r) => {
    let body = "";
    r.on("data", (c) => (body += c));
    r.on("end", () => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: r.statusCode === 200, lock: body }));
    });
  });
  req.on("timeout", () => req.destroy(new Error("timeout")));
  req.on("error", (e) => {
    res.writeHead(502, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: String(e.message || e) }));
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (req.method === "POST" && url.pathname === "/api/snapshot") {
    latestSnapshot = await readBody(req);
    latestSnapshotAt = Date.now();
    res.writeHead(200).end("ok");

  } else if (req.method === "GET" && url.pathname === "/api/snapshot") {
    if (!latestSnapshot) return res.writeHead(404).end();
    res.writeHead(200, {
      "Content-Type": "image/jpeg",
      "Cache-Control": "no-store",
      "X-Snapshot-Age-Ms": String(Date.now() - latestSnapshotAt),
    });
    res.end(latestSnapshot);

  } else if (req.method === "POST" && url.pathname === "/api/event") {
    const body = await readBody(req);
    let payload;
    try {
      payload = JSON.parse(body.toString("utf8"));
    } catch {
      return res.writeHead(400).end("bad json");
    }
    const events = loadEvents();
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let imageFile = null;
    if (payload.image_b64) {
      imageFile = `${id}.jpg`;
      fs.writeFileSync(path.join(IMAGES_DIR, imageFile), Buffer.from(payload.image_b64, "base64"));
    }
    events.unshift({ id, event: payload.event || "unknown", at: Date.now(), imageFile });
    fs.writeFileSync(EVENTS_FILE, JSON.stringify(events.slice(0, 500), null, 1));
    res.writeHead(200).end("ok");

  } else if (req.method === "GET" && url.pathname === "/api/events") {
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    res.end(JSON.stringify(loadEvents()));

  } else if (req.method === "GET" && url.pathname.startsWith("/event-images/")) {
    const file = path.join(IMAGES_DIR, path.basename(url.pathname));
    fs.readFile(file, (err, data) => {
      if (err) return res.writeHead(404).end();
      res.writeHead(200, { "Content-Type": "image/jpeg" }).end(data);
    });

  } else if (req.method === "GET" && url.pathname === "/api/open") {
    proxyLock("/open", res);
  } else if (req.method === "GET" && url.pathname === "/api/pulse") {
    proxyLock("/pulse", res);
  } else if (req.method === "GET" && url.pathname === "/api/lockstatus") {
    proxyLock("/status", res);

  } else if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
    fs.readFile(path.join(DIR, "index.html"), (err, data) => {
      if (err) return res.writeHead(500).end("missing index.html");
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }).end(data);
    });

  } else {
    res.writeHead(404).end();
  }
});

server.listen(PORT, () => {
  console.log(`Porch Lockbox dashboard: http://localhost:${PORT}`);
  console.log(`On your phones (same Wi-Fi): http://<this-mac's-ip>:${PORT}`);
  console.log(`ESP32: ${esp32Host() || "NOT CONFIGURED (.env)"}`);
});

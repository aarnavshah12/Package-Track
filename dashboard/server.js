// Porch Lockbox dashboard server - zero dependencies.
// Runs identically on the Mac (LaunchAgent) or any cloud host (Render/Fly/etc).
//
//   node dashboard/server.js
//
// Env (all optional):
//   PORT        - default 8321
//   DASH_TOKEN  - shared secret; if unset, read from ../.env; if still unset,
//                 the server runs OPEN (fine on a home LAN, never on the cloud)
//
// The camera phone POSTs frames + event photos here. Browsers watch a real
// MJPEG stream (/api/stream). Lock commands try the ESP32 directly when this
// server is on the home LAN; otherwise they queue for the camera phone to
// execute (the phone polls /api/streamctl and is always on the home Wi-Fi).

const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = process.env.PORT || 8321;
const DIR = __dirname;
const EVENTS_FILE = path.join(DIR, "events.json");
const IMAGES_DIR = path.join(DIR, "event-images");
fs.mkdirSync(IMAGES_DIR, { recursive: true });

function envFile() {
  try { return fs.readFileSync(path.join(DIR, "..", ".env"), "utf8"); } catch { return ""; }
}
function envVar(name) {
  const m = envFile().match(new RegExp(`^${name}=(.+)$`, "m"));
  return m ? m[1].trim() : null;
}
const TOKEN = process.env.DASH_TOKEN || envVar("DASH_TOKEN") || "";
const ESP32 = process.env.ESP32_IP || envVar("ESP32_IP") || null;

let latestSnapshot = null;
let latestSnapshotAt = 0;
const streamClients = new Set();   // open MJPEG responses
const pendingCommands = [];        // [{id, cmd, at}] awaiting the camera phone

function authorized(req, url) {
  if (!TOKEN) return true;
  return req.headers["x-lockbox-token"] === TOKEN || url.searchParams.get("key") === TOKEN;
}

function loadEvents() {
  try { return JSON.parse(fs.readFileSync(EVENTS_FILE, "utf8")); } catch { return []; }
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
  });
}

function broadcastFrame(jpeg) {
  for (const res of streamClients) {
    try {
      res.write(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.length}\r\n\r\n`);
      res.write(jpeg);
      res.write("\r\n");
    } catch {
      streamClients.delete(res);
    }
  }
}

function tryESP32(pathname) {
  return new Promise((resolve) => {
    if (!ESP32) return resolve(false);
    const req = http.get({ host: ESP32.split(":")[0], port: 80, path: pathname, timeout: 2500 },
      (r) => { r.resume(); resolve(r.statusCode === 200); });
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}

const COMMAND_PATHS = { open: "/open", pulse: "/pulse" };

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const p = url.pathname;

  // The page shell is public; every data endpoint requires the token.
  if (p === "/" || p === "/index.html") {
    return fs.readFile(path.join(DIR, "index.html"), (err, data) => {
      if (err) return res.writeHead(500).end("missing index.html");
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }).end(data);
    });
  }
  if (!authorized(req, url)) {
    return res.writeHead(401, { "Content-Type": "application/json" })
      .end(JSON.stringify({ error: "missing or wrong key" }));
  }

  if (req.method === "POST" && p === "/api/snapshot") {
    latestSnapshot = await readBody(req);
    latestSnapshotAt = Date.now();
    broadcastFrame(latestSnapshot);
    res.writeHead(200).end("ok");

  } else if (req.method === "GET" && p === "/api/stream") {
    res.writeHead(200, {
      "Content-Type": "multipart/x-mixed-replace; boundary=frame",
      "Cache-Control": "no-store",
      Connection: "close",
    });
    streamClients.add(res);
    res.on("error", () => streamClients.delete(res));
    if (latestSnapshot) broadcastFrame(latestSnapshot);
    req.on("close", () => streamClients.delete(res));

  } else if (req.method === "GET" && p === "/api/snapshot") {
    if (!latestSnapshot) return res.writeHead(404).end();
    res.writeHead(200, {
      "Content-Type": "image/jpeg", "Cache-Control": "no-store",
      "X-Snapshot-Age-Ms": String(Date.now() - latestSnapshotAt),
    }).end(latestSnapshot);

  } else if (req.method === "POST" && p === "/api/event") {
    let payload;
    try { payload = JSON.parse((await readBody(req)).toString("utf8")); }
    catch { return res.writeHead(400).end("bad json"); }
    const events = loadEvents();
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let imageFile = null;
    if (payload.image_b64) {
      imageFile = `${id}.jpg`;
      fs.writeFileSync(path.join(IMAGES_DIR, imageFile), Buffer.from(payload.image_b64, "base64"));
    }
    events.unshift({ id, event: payload.event || "unknown", at: Date.now(), imageFile });
    // Keep the newest 200; delete the photos of anything evicted so disk
    // usage stays bounded on small hosts.
    const kept = events.slice(0, 200);
    for (const evicted of events.slice(200)) {
      if (evicted.imageFile) {
        fs.unlink(path.join(IMAGES_DIR, evicted.imageFile), () => {});
      }
    }
    fs.writeFileSync(EVENTS_FILE, JSON.stringify(kept, null, 1));
    res.writeHead(200).end("ok");

  } else if (req.method === "GET" && p === "/api/events") {
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" })
      .end(JSON.stringify(loadEvents()));

  } else if (req.method === "GET" && p.startsWith("/event-images/")) {
    fs.readFile(path.join(IMAGES_DIR, path.basename(p)), (err, data) => {
      if (err) return res.writeHead(404).end();
      res.writeHead(200, { "Content-Type": "image/jpeg" }).end(data);
    });

  } else if (req.method === "POST" && p === "/api/command") {
    let cmd;
    try { cmd = JSON.parse((await readBody(req)).toString("utf8")).cmd; }
    catch { return res.writeHead(400).end("bad json"); }
    if (!["open", "pulse", "box_emptied"].includes(cmd)) return res.writeHead(400).end("unknown cmd");
    // Direct ESP32 path when this server is on the home LAN; otherwise the
    // camera phone (always home) picks the command up within a few seconds.
    if (COMMAND_PATHS[cmd] && (await tryESP32(COMMAND_PATHS[cmd]))) {
      res.writeHead(200, { "Content-Type": "application/json" })
        .end(JSON.stringify({ ok: true, via: "direct" }));
    } else {
      pendingCommands.push({ id: Date.now(), cmd, at: Date.now() });
      res.writeHead(200, { "Content-Type": "application/json" })
        .end(JSON.stringify({ ok: true, via: "phone", note: "queued for the camera phone" }));
    }

  } else if (req.method === "GET" && p === "/api/streamctl") {
    // Camera phone heartbeat: learns whether anyone is watching (to raise its
    // mirror FPS) and drains queued commands (to execute on the LAN).
    const drained = pendingCommands.splice(0, pendingCommands.length)
      .filter((c) => Date.now() - c.at < 60_000)   // stale commands die
      .map((c) => c.cmd);
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" })
      .end(JSON.stringify({ viewers: streamClients.size, commands: drained }));

  } else if (req.method === "GET" && p === "/api/lockstatus") {
    if (!ESP32) {
      return res.writeHead(200, { "Content-Type": "application/json" })
        .end(JSON.stringify({ ok: false, error: "remote mode" }));
    }
    const ok = await new Promise((resolve) => {
      const r = http.get({ host: ESP32.split(":")[0], port: 80, path: "/status", timeout: 2500 }, (rr) => {
        let b = ""; rr.on("data", (c) => (b += c));
        rr.on("end", () => resolve(b));
      });
      r.on("timeout", () => { r.destroy(); resolve(null); });
      r.on("error", () => resolve(null));
    });
    res.writeHead(200, { "Content-Type": "application/json" })
      .end(JSON.stringify(ok ? { ok: true, lock: ok } : { ok: false, error: "lock unreachable" }));

  } else {
    res.writeHead(404).end();
  }
});

server.listen(PORT, () => {
  console.log(`Porch Lockbox dashboard on :${PORT}`);
  console.log(`auth: ${TOKEN ? "token required" : "OPEN (no DASH_TOKEN set)"}`);
  console.log(`ESP32 direct path: ${ESP32 || "no (remote mode - commands queue for the phone)"}`);
});

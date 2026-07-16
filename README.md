# Porch package lockbox

A self-opening drop box for deliveries. An old iPhone at a window watches the porch with an on-device detection model. When a courier arrives with a package, a cloud Roboflow Workflow confirms it, the phone unlocks the box over home Wi-Fi, the courier drops the package in, the box re-locks itself, and the system verifies the delivery and pushes a photo notification to your phone. A token-protected dashboard shows the live stream and delivery history from anywhere.

```
 iPhone at window                     Roboflow serverless cloud
┌──────────────────────────┐  1 fps,  ┌─────────────────────────────┐
│ on-device model (25 fps) │  only    │ workflow: detect → zone →   │
│ wake gate + state machine│ when ───▶│ per-frame facts             │
│                          │ awake    │ + event-gated sinks:        │
│      │ GET /open         │ ◀─ facts │ ntfy push · vision events · │
│      ▼   (LAN only)      │          │ dataset upload (retraining) │
│ ESP32 + relay + solenoid │          └─────────────────────────────┘
└──────────┬───────────────┘
           │ frames + event photos            browsers anywhere
           ▼                                        ▲
      dashboard server (LAN or Render) ── MJPEG ────┘
```

Design rules:

- **Nothing on the internet can reach the lock.** The ESP32 answers only on the home LAN; the only caller is the phone standing next to it. Remote dashboard buttons work by queueing a command that the phone picks up and executes locally.
- **The cloud is stateless; the phone owns all state.** The workflow judges single frames and returns booleans; dwell timing, the unlock latch, grace windows, and verification live in the client state machine.
- **The cloud only runs when something happens.** The on-device model gates streaming: a vehicle arriving, a person + package, or a lingering person wakes it; 30 quiet seconds puts it back to sleep. Unlocking always requires cloud-confirmed facts, never the on-device opinion alone.

## What's in the repo

| Path | What it is |
|---|---|
| `esp32_lockbox/esp32_lockbox.ino` | Lock firmware: Wi-Fi web server with `/open`, `/pulse`, `/toggle`, `/status`, mDNS (`lockbox.local`), self-closing open window, 30 s thermal watchdog |
| `workflow_spec.json` | The complete cloud workflow definition (paste into the Roboflow Workflows JSON editor) |
| `ios/Roboflow Starter Project/` | The camera/brain iPhone app (Xcode project, Swift Package Manager) |
| `dashboard/` | Zero-dependency Node server + single-page UI: MJPEG live stream, event gallery, remote lock buttons |
| `render.yaml` | One-click Render deploy blueprint for the dashboard |
| `lockbox_client.py` / `lockbox_config.py` | The same brain as a Python script for a Mac webcam — test the full pipeline without the phone. Also a still-photo test tool (`--image`) and offline test suite (`--selftest`) |
| `.env.example` | Template for local secrets (`ROBOFLOW_API_KEY`, `ESP32_IP`, `NTFY_TOPIC`, `DASH_TOKEN`) |

**Secrets convention:** nothing sensitive is committed. Every file that needs a credential has a committed template next to it — `.env.example` → `.env`, `esp32_lockbox/credentials.example.h` → `credentials.h`, `ios/.../LockboxSecrets.example.swift.txt` → `LockboxSecrets.swift`. The real files are gitignored.

## Hardware

| Part | Notes |
|---|---|
| ESP32 dev board | Any devkit clone |
| 5V single-channel relay module | One with a high/low-level trigger jumper |
| 12V **fail-secure** solenoid lock | Spring-extended bolt; energize to retract. Unpowered = locked |
| 12V 2A DC adapter | Powers the solenoid through the relay |
| Box + wooden flap + hinge + flat furniture braces | Braces stop the flap being pushed inward AND act as the strike plate the bolt locks over |
| Old iPhone (XS or newer) + window mount | The camera/brain |

Wiring — two circuits that never share a wire:

```
ESP32 5V   → relay VCC          12V adapter (+) → relay COM
ESP32 GND  → relay GND          relay NO        → solenoid (+)
ESP32 GPIO4→ relay IN           solenoid (−)    → 12V adapter (−)
```

Use the relay's **NO** (normally open) terminal so the coil is cold and the box is locked by default. After flashing, the relay LED must be **off** at idle — if it isn't, move the trigger jumper to the other position or flip `RELAY_ACTIVE_LOW` at the top of the firmware (the constant and the jumper must agree).

## Setup

Do these in order. Each step is independently testable.

### 1. Flash the ESP32

1. Open `esp32_lockbox/esp32_lockbox.ino` in Arduino IDE (install ESP32 board support if needed).
2. `cp credentials.example.h credentials.h` and enter your Wi-Fi name/password.
3. Select your board + port and upload. (If upload fails at high speed, drop Upload Speed to 115200 and hold BOOT during the "Connecting…" dots.)
4. Test from any browser on your Wi-Fi: `http://lockbox.local/pulse` should clunk the bolt for 1 s. `http://lockbox.local/` serves a minimal control page; `/status` returns JSON.

Endpoints: `/open` (13 s delivery window, closes on its own timer), `/pulse` (1 s test), `/toggle` (manual, 30 s auto-relock), `/status`. The firmware also force-releases the coil after 30 s continuous — solenoids are pulse-duty parts.

Tip: give the ESP32 a DHCP reservation in your router so its IP never changes, or use `lockbox.local` everywhere.

### 2. Train the model

1. Create a free [Roboflow](https://roboflow.com) project; start from a packages dataset on [Universe](https://universe.roboflow.com) and add photos of **your own porch** from the camera's actual mounting position (you holding a box, box on the ground, empty porch).
2. Train — RF-DETR works well. You get a model ID like `your-project/1`. **Use the two-segment `project/version` form everywhere** — the API rejects workspace-prefixed IDs.
3. Note your model's class names (visible in the training results). If it emits numeric names, list them as-is in the configs below; the workflow renames them.

### 3. Trace your porch zone

1. Grab a frame from the mounted camera and outline your porch in [Polygon Zone](https://polygonzone.roboflow.com). Draw it where couriers *stand*, not just where packages land — a person's center point sits waist-high.
2. Normalize the pixel points (divide x by image width, y by height) to get 0–1 fractions, e.g. `[[0.53, 0.55], [0.29, 0.60], ...]`.
3. That list goes into `LockboxConfig.swift` (`zoneNormalized`) and/or `lockbox_config.py` (`PORCH_ZONE`). Clients rescale it to each frame at request time — the workflow itself never needs editing when the camera moves.

### 4. Deploy the workflow

1. In Roboflow: Workflows → Create → open the JSON editor → paste `workflow_spec.json`.
2. Replace the personal values with yours: the detection step's `model_id` default, `raw_classes` (your model's class names), the rename map (only needed for numeric class names), the dataset-upload `target_project`, and the Vision Events use case.
3. Save/deploy. Your endpoint is `https://serverless.roboflow.com/infer/workflows/<workspace>/<workflow-slug>`.
4. Test in the editor preview with still photos: you holding a box inside the zone should return `person_with_package: true`.

The pipeline: detection → class filter → rename → zone filter (center-in-polygon) → a small Python facts block → visualizations → three sinks gated on `client_event` (ntfy webhook push, Vision Events history, dataset upload for retraining). Regular frames return facts only; when the client reaches a verdict it re-sends that one frame with `client_event` set, which fires all three sinks exactly once.

Request parameters (the client sends all of these on every call): `model_id`, `zone` (pixel coords of the sent frame), `min_confidence` (0.4), `person_confidence` (0.45), `package_confidence` (0.59), `raw_classes`, `client_event` (`none` | `delivery_confirmed` | `delivery_failed_package_on_ground`), `disable_upload` (`false` only on event calls), `ntfy_topic`.

Response facts: `person_in_zone`, `package_in_zone`, `person_with_package`, `person_count`, `package_count`, `max_person_confidence`, `max_package_confidence`, `boxes`, `event_ack`.

Note: the serverless API caches workflow definitions for ~15 minutes — after editing the workflow, either wait or send `use_cache: false` while testing.

### 5. Build the iPhone app

Requirements: a Mac with Xcode, an Apple ID, an iPhone on iOS 16.6+ (XS or newer).

1. Open `ios/Roboflow Starter Project/Roboflow Starter Project.xcodeproj`. Xcode auto-resolves the single dependency (`roboflow-swift`, via Swift Package Manager) on first open.
2. In `ViewController.swift`, set `API_KEY` (your **publishable** `rf_...` key — safe to embed; it can only download models), `MODEL`, and `VERSION`. On first launch the SDK downloads a CoreML build of your model to the phone and caches it; detection then runs fully on-device.
3. In `LockboxConfig.swift`, set `workflowURL` (your workspace + workflow slug), `modelId`, `rawClasses`, `zoneNormalized`, and review the timings. `graceSeconds` ships at 15 for bench testing — set it to ~90 for a real porch.
4. Copy `LockboxSecrets.example.swift.txt` → `LockboxSecrets.swift` (gitignored) and fill in: your **private** Roboflow API key (used for workflow calls), the ESP32 host, your ntfy topic, and the dashboard URL + token.
5. Signing & Capabilities → Automatically manage signing → select your team (add your Apple ID under Xcode Settings → Accounts) → set a unique bundle identifier.
6. On the phone: enable Developer Mode (Settings → Privacy & Security), plug in, select the phone as run destination, press ▶. First install: Settings → General → VPN & Device Management → Trust. Free Apple IDs re-sign every 7 days (press ▶ again); a paid developer account makes it yearly.
7. Mount the phone at a window facing the porch and plug it into power. The app keeps its own screen awake.

The app is the wake gate (streams to the cloud only on a vehicle arrival, person+package, or a lingering person), the state machine (dwell → 5 s countdown → `/open` → verification vote → event), and the dashboard's frame source. The camera phone's screen deliberately has **no unlock button** — it's the one device a stranger could touch, so all control lives behind the dashboard token.

### 6. Run the dashboard

Local (same LAN as the ESP32 — buttons hit the lock directly):

```bash
DASH_TOKEN=$(openssl rand -hex 8) node dashboard/server.js   # or put DASH_TOKEN in .env
# open http://localhost:8321 and enter the token
```

Cloud (watch from anywhere — buttons relay through the phone):

1. Push your fork to GitHub, then on [render.com](https://render.com): New → Blueprint → connect the repo. Render reads `render.yaml`.
2. Enter `DASH_TOKEN` when prompted (it never lives in the repo).
3. Put the resulting URL + token into `LockboxSecrets.swift` and rebuild the app. The phone starts mirroring on its own.

How it works: the phone POSTs JPEG frames; the server rebroadcasts them to browsers as an MJPEG stream. The phone checks in every 3 s — if nobody is watching it sends 1 frame per 20 s, and it ramps to ~8 fps when a viewer connects. The same check-in drains queued button commands (`open` / `pulse` / `box_emptied`), which the phone executes on the LAN; unclaimed commands expire after 60 s.

### 7. Turn on notifications

1. Pick a long, unguessable topic name — it acts as a password (e.g. `porch-lockbox-a1b2c3`).
2. Put it in `.env` (`NTFY_TOPIC`) and `LockboxSecrets.swift` (`ntfyTopic`).
3. Install the [ntfy](https://ntfy.sh) app on every phone that should get alerts and subscribe to the topic.

Both verdicts push, with a photo: *delivered* ✅ and *left outside* ⚠️.

### 8. (Optional) Test everything with a Mac webcam first

`lockbox_client.py` is the whole phone app as a Python script — useful for proving the pipeline before the phone or box exist:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # private API key, ESP32 IP, ntfy topic
python3 lockbox_client.py --selftest    # offline state-machine tests
python3 lockbox_client.py               # live: webcam → cloud → ESP32
python3 lockbox_client.py --image photo.jpg   # single still through the workflow
```

Walk into frame holding a cardboard box: the console prints the countdown, the solenoid clunks, and after the grace window you get exactly one delivery event. Keys: `e` marks the box emptied, `u` manual pulse, `q` quits.

## The delivery sequence

1. On-device model spots a wake trigger → phone streams 1 fps to the workflow.
2. Cloud confirms person + package inside the zone for 3 consecutive frames (one missed frame forgiven).
3. 5-second countdown (there's a sign on the box: "Delivery detected — box opens in 5 seconds").
4. Phone calls `GET /open`; the ESP32 holds the bolt open 13 s and re-locks on its own timer.
5. After the grace period, 3 cloud frames vote: zone clear → `delivery_confirmed`; package still visible → `delivery_failed_package_on_ground` (the "go rescue it" alert). A courier returning mid-verification restarts the grace window (capped at 2 extensions).
6. The verdict frame is re-sent event-tagged → one ntfy push + one Vision Event + one dataset upload (tag `lockbox-event`) for retraining.
7. 60 s cooldown, then re-armed. A `package_in_box` flag persists across restarts until you press the box-emptied button.

## License / disclaimer

Personal project; use at your own risk. A solenoid parcel box deters casual theft — it is not a safe.

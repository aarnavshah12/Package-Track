# Porch Pirate Defense Lockbox — Vision System

A window-mounted camera watches the porch. When a courier arrives with a package, a
cloud Roboflow Workflow detects it, this client unlocks the drop box over the LAN,
verifies the package went inside, re-locks, and records the event with photos —
into Roboflow Vision Events (the future app's alert/history feed), the dataset
(retraining), and a local `events/` folder.

```
camera client (Mac now, iPhone later)          Roboflow serverless cloud
┌─────────────────────────────────┐   frames   ┌────────────────────────────────┐
│ wake gate (motion / on-device)  │ ─ ~1 fps ─▶│ workflow: aarnavs-space/       │
│ STATE MACHINE (dwell, latch,    │ ◀─ facts ──│   package-track                │
│ grace timer, verify, dedup)     │            │ detect → rename → zone →       │
│        │ GET /pulse             │   event-   │ per-frame facts + gated sinks  │
│        ▼                        │   tagged   │ (vision events = alert feed,   │
│ ESP32 + solenoid (LAN only)     │ ─ frame ──▶│  dataset upload = retraining)  │
└─────────────────────────────────┘            └────────────────────────────────┘
```

**Why the state machine is client-side (resolved, not assumed):** the hosted
serverless Workflow API is stateless per request — Roboflow's own docs state that
stateful video blocks reset between HTTP calls and that sink `cooldown_seconds`
"will have no effect for processing HTTP requests". So the Workflow returns clean
**per-frame facts** and this client owns dwell counting, the unlock latch, the
grace timer, `package_in_box`, and one-event-one-notification dedup. (Server-side
state would require an InferencePipeline video deployment — a documented upgrade
path, not needed for v1.) Notifications still fire *server-side*: when the client
decides a terminal event happened, it re-sends that one deciding frame with
`client_event` set, which un-gates the vision-event / dataset-upload branch
exactly once. (An email block existed here originally per the first spec; it was
descoped 2026-07-14 — the final product surfaces alerts in the app via Vision
Events instead.)

## Files

| File | Purpose |
|---|---|
| `lockbox_client.py` | Phase B client: webcam loop, state machine, ESP32, events. Also the Phase A photo-test tool (`--image`) and offline test suite (`--selftest`, 12 scenarios). |
| `lockbox_config.py` | Every tunable (model, zone, confidences, dwell, grace, gate, ESP32 timing). |
| `workflow_spec.json` | Source of truth for the deployed workflow definition. |
| `.env` (create from `.env.example`) | `ROBOFLOW_API_KEY` (private key) + `ESP32_IP`. Never commit. |
| `events/` | Triggering frames + `events.jsonl` (history feed + retraining data). |
| `state.json` | Persists `package_in_box` across restarts. |

## Setup (one time)

```bash
# 0. Accept the Xcode license — the system python3 and git are blocked until you do:
sudo xcodebuild -license accept

# 1. Python deps
cd "~/Documents/Package Track"
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Secrets
cp .env.example .env   # then edit: your PRIVATE Roboflow API key + the ESP32's LAN IP

# 3. Sanity check (no camera/network needed) — expect "12/12 passed"
python3 lockbox_client.py --selftest

# 4. (Recommended) git — the repo isn't initialized yet because of the license block:
git init && git add -A && git commit -m "phase A+B"
```

## The deployed Workflow (Phase A)

- **Editor:** https://app.roboflow.com/aarnavs-space/workflows/edit/package-track
- **Endpoint:** `POST https://serverless.roboflow.com/infer/workflows/aarnavs-space/package-track`
  ⚠️ The slug changed from `package` to `package-track` during the headless update
  (the API regenerates the slug from the name). Nothing had shipped against the old
  URL; everything here uses `package-track`.

**Pipeline:** RF-DETR detection (`aarnavs-space/package-goilk-zcar8/1`) → keep raw
classes `["0","80"]` → rename `0→person`, `80→package` (the model emits *numeric*
class names; this is why the old draft's `class_filter: ["package","person"]`
detected nothing) → keep detections whose **center** is inside the `zone` polygon
(pixel coords, per-request parameter) → custom Python facts block (stateless) →
visualization → two sinks gated on `client_event`/`disable_upload` (Vision Events + dataset upload).

**Request parameters** (all optional; defaults in parentheses — the client sends
all of them from `lockbox_config.py` on every call):

| Parameter | Default | Notes |
|---|---|---|
| `model_id` | `package-goilk-zcar8/1` | format is `project/version` — NO workspace prefix (the server rejects 3-segment ids). Swappable, e.g. `bearbox/18` (then set `raw_classes` to `["package","person"]`) |
| `zone` | pass-everything polygon | pixel coords of the frame you send, ≥3 `[x,y]` points |
| `min_confidence` | 0.4 | model-level floor |
| `person_confidence` / `package_confidence` | 0.45 / 0.59 | per-class thresholds (F1-optimal on the eval split) |
| `raw_classes` | `["0","80"]` | class names the model emits, pre-rename |
| `client_event` | `"none"` | `delivery_confirmed` \| `delivery_failed_package_on_ground` un-gates the vision event |
| `disable_upload` | `true` | client sets `false` on event calls → dataset upload for retraining |
| `notify_email` | aarnav.shah@roboflow.com | unused since the email descope; kept for compatibility |

**Sample workflow response** (real output; per-frame calls exclude `output_image`):

```json
{
  "person_in_zone": true, "package_in_zone": false, "person_with_package": false,
  "person_count": 23, "package_count": 0,
  "max_person_confidence": 0.813, "max_package_confidence": 0,
  "boxes": [{"class": "person", "confidence": 0.813,
             "box": {"x_min": 1460, "y_min": 469, "x_max": 1544, "y_max": 641},
             "center": {"x": 1502, "y": 555}}, "…"],
  "event_ack": "none",
  "predictions": {"…": "full inference-format detections (renamed classes)"},
  "zone_predictions": {"…": "only detections inside the zone"}
}
```

**App contract** (what a future consumer app reads — composed by the client, since
`unlock`/`state`/`package_in_box`/`event` are client-owned):

```json
{
  "unlock": true, "state": "UNLOCK_HOLD",
  "package_in_zone": true, "person_in_zone": true, "person_with_package": true,
  "package_in_box": false, "event": null,
  "raw": {"boxes": ["…"], "person_count": 1, "package_count": 1,
          "max_person_confidence": 0.91, "max_package_confidence": 0.84}
}
```

## State machine (client-side)

`ARMED → WAIT_OPEN → UNLOCK_HOLD → VERIFYING → ARMED`, plus a persisted `package_in_box` flag.

- **ARMED** — motion gate may skip cloud calls. `person_with_package` for
  `DWELL_FRAMES` (3) consecutive frames (1 missed frame tolerated) → WAIT_OPEN.
- **WAIT_OPEN** — courier-courtesy delay: `PRE_OPEN_SECONDS` (5) for the courier
  to read the porch sign and step to the box, then `GET http://ESP32_IP/open`
  (retried until it succeeds).
- **UNLOCK_HOLD** — the ESP32 physically holds the bolt open for
  `BOX_OPEN_SECONDS` (13, = `OPEN_HOLD_MS` in the firmware) and re-locks it on
  its own timer — the client never has to remember to close it, and the firmware's
  30 s thermal watchdog caps coil time in any failure mode. Logically the client
  stays latched for `GRACE_SECONDS` (90) before verification.
- **VERIFYING** — gate bypassed (frames always stream). Courier back with the
  package → grace restarts (max 2 extensions, then loitering counts as failure).
  Otherwise majority vote over `VERIFY_FRAMES` (3): package still visible →
  `delivery_failed_package_on_ground` (alert event); clear →
  `delivery_confirmed` (success event) + `package_in_box=true` until you press
  `e` (or `--reset-box`). Sustained network failure → conservative
  `package_in_box=true`, recorded locally as `verification_inconclusive`.
- After a terminal event: `EVENT_COOLDOWN_SECONDS` (60) suppresses re-triggering.

Every unlock/event saves a timestamped frame in `events/` + a line in
`events.jsonl`. Cloud-side, every event also creates a **Vision Event** (use case
"Package Lockbox Deliveries" — the future app's history feed, ✅ verified live) and a
**dataset upload** tagged `lockbox-event` into `package-goilk-zcar8` (retraining pool).

## Phase A milestone — your still-photo tests

Take 4 photos at the planned camera angle, then run each through:

```bash
python3 lockbox_client.py --image porch-photos/person-with-box.jpg
```

| Photo | person_in_zone | package_in_zone | person_with_package |
|---|---|---|---|
| courier holding box in zone | true | true | true |
| box on ground, nobody | false | true | false |
| empty porch | false | false | false |
| person without box | true | false | false |

Then one notification test (logs ONE vision event + one dataset upload):

```bash
python3 lockbox_client.py --image porch-photos/box-on-ground.jpg --event delivery_failed_package_on_ground
```

**Pass:** booleans match the table; exactly one new vision event in the
"Package Lockbox Deliveries" use case (app.roboflow.com → Vision Events); one
`lockbox-event`-tagged image in the project. If the zone booleans look wrong,
tune `ZONE`/`PORCH_ZONE` in `lockbox_config.py` (normalized 0-1 coords;
`None` = central 70% of the frame). ✅ Completed 2026-07-14 with 14 porch photos.

## Phase B milestone — the physical loop

`python3 lockbox_client.py` with the Mac webcam aimed at your test area, ESP32 live:

1. **Happy path:** walk in holding a cardboard box → within ~3 s the console prints
   `>>> UNLOCK` and the solenoid clicks once → "deliver" the box out of view →
   ~90 s later exactly one `*** EVENT delivery_confirmed` banner, a new vision
   event with the photo, `package_in_box=true` badge, and a frame in `events/`.
2. **No box:** walk through empty-handed → no unlock ever.
3. **Failure path:** press `e`, repeat with the box, but leave it visible on the
   ground → after the grace window, one `delivery_failed_package_on_ground` event, no unlock during verification.
4. **Edge checks:** restart the script (flag persists) · unplug the ESP32 and
   trigger (ERROR logged, stays ARMED, no crash) · kill Wi-Fi during verification
   (conservative inconclusive path).

The preview window renders at full camera speed (~30 fps); only `TARGET_FPS` (1)
frame per second goes to the cloud, so overlays update about once a second while
the video itself stays smooth. On quit the client prints `sample ticks vs frames
sent` — the wake-word cost-efficiency metric for the Phase D writeup.

## Known caveats

1. **Email was descoped (2026-07-14):** the final product surfaces alerts inside
   the app (live view + popups + manual unlock), so the email block was removed
   from the workflow. Alerts/history live in Vision Events (use case "Package
   Lockbox Deliveries", queryable by API). Historical note: Roboflow's managed
   email proxy 500s even for workspace-member recipients — reported-worthy bug.
2. **`use_cache`:** the serverless API caches workflow definitions ~15 min. After
   editing the workflow, either wait or flip `"use_cache": False` temporarily in
   `WorkflowClient.infer`.
3. **HOG gate** misses close-range/partial bodies (it would fail your own webcam
   demo) — that's why `GATE_MODE="motion"` is the default. The iPhone (Phase C)
   replaces this with a real on-device person model.

## Phase C/D preview

- **C (iOS):** fork `roboflow-ios-starter` (CocoaPods `pod 'Roboflow'`); on-device
  person model via `rf.load(model:modelVersion:)` becomes the wake gate (person ≥2
  frames → stream at 1 fps; absent 30 s → stop); port `LockboxStateMachine` 1:1 to
  a Swift struct (it's deliberately a pure function of facts + clock); URLSession
  POST of the same JSON payload; `package_in_box` in UserDefaults.
- **D:** tune zone/thresholds on real porch frames; retrain on the
  `lockbox-event`-tagged uploads; metrics = session gate stats + model evals
  before/after porch fine-tuning.

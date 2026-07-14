#!/usr/bin/env python3
"""Porch Package Lockbox - Phase B client (Mac webcam stand-in for the iPhone).

Streams webcam frames (~1 fps) to the Roboflow workflow, runs the client-side
state machine (dwell -> unlock latch -> grace -> verify), pulses the ESP32 lock
over the LAN, and triggers server-side notifications by re-sending the deciding
frame tagged with client_event. The cloud never touches the LAN; this client is
the only thing that talks to the ESP32.

The workflow is stateless per request (serverless), so ALL cross-frame state
lives here: dwell counting, the unlock latch, the grace timer, verification
voting, package_in_box, and one-event-one-notification dedup.

Modes:
  python3 lockbox_client.py                                   live webcam loop
  python3 lockbox_client.py --image porch.jpg                 single-shot facts (Phase A photo tests)
  python3 lockbox_client.py --image porch.jpg --event delivery_confirmed
                                                              single-shot event: fires email + vision event + dataset upload
  python3 lockbox_client.py --selftest                        offline state-machine tests (no camera, no network)
  python3 lockbox_client.py --reset-box                       clear the persisted package_in_box flag

Keys in the preview window: q quit | e box emptied (reset package_in_box) | u manual pulse
"""

import argparse
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

import lockbox_config as cfg

BASE_DIR = Path(__file__).resolve().parent

VALID_EVENTS = ("delivery_confirmed", "delivery_failed_package_on_ground")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_env():
    """Read ROBOFLOW_API_KEY and ESP32_IP from .env (python-dotenv optional)."""
    env_path = BASE_DIR / ".env"
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
    return os.environ.get("ROBOFLOW_API_KEY", ""), os.environ.get("ESP32_IP", "")


def prepare_frame(frame):
    """Downscale big frames (e.g. 12 MP iPhone photos) before anything else.
    Keeps zone coords, previews, saved events, and cloud payloads in one
    consistent pixel space, and cuts upload size/latency ~10x."""
    import cv2
    height, width = frame.shape[:2]
    longest = max(height, width)
    if longest <= cfg.MAX_FRAME_DIM:
        return frame
    scale = cfg.MAX_FRAME_DIM / longest
    return cv2.resize(frame, (int(width * scale), int(height * scale)))


def central_zone(width, height, fraction=0.7):
    """Axis-aligned central `fraction` of the frame as a pixel polygon."""
    mx = int(width * (1 - fraction) / 2)
    my = int(height * (1 - fraction) / 2)
    return [[mx, my], [width - mx, my], [width - mx, height - my], [mx, height - my]]


def resolve_zone(width, height):
    """cfg.ZONE -> pixel polygon for a width x height frame. Accepts None
    (central 70%), normalized 0-1 fractions, or raw pixel coordinates."""
    if not cfg.ZONE:
        return central_zone(width, height)
    if all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in cfg.ZONE):
        return [[int(round(x * width)), int(round(y * height))] for x, y in cfg.ZONE]
    return [[int(x), int(y)] for x, y in cfg.ZONE]


# --------------------------------------------------------------------------
# Workflow client (plain HTTP: same call the iOS app will make in Phase C)
# --------------------------------------------------------------------------
class WorkflowClient:
    def __init__(self, api_key):
        if not api_key:
            raise SystemExit("ROBOFLOW_API_KEY missing - copy .env.example to .env and fill it in.")
        self.api_key = api_key
        self.url = f"{cfg.ROBOFLOW_API_URL}/infer/workflows/{cfg.WORKSPACE}/{cfg.WORKFLOW_ID}"

    def infer(self, frame_bgr, zone, client_event="none", include_image=False):
        """One workflow call. Returns (facts dict, round-trip seconds).

        client_event != "none" fires the notification branch server-side
        (email + vision event + dataset upload) exactly once - only the client
        decides when that happens, which is what makes one event == one email.
        """
        import cv2
        import requests

        ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        is_event = client_event in VALID_EVENTS
        payload = {
            "api_key": self.api_key,
            "use_cache": True,
            "inputs": {
                "image": {"type": "base64", "value": base64.b64encode(buf.tobytes()).decode("ascii")},
                "model_id": cfg.MODEL_ID,
                "zone": zone,
                "min_confidence": cfg.MIN_CONFIDENCE,
                "person_confidence": cfg.PERSON_CONFIDENCE,
                "package_confidence": cfg.PACKAGE_CONFIDENCE,
                "raw_classes": cfg.RAW_CLASSES,
                "client_event": client_event,
                "disable_upload": not is_event,
                "notify_email": cfg.NOTIFY_EMAIL,
            },
        }
        if not include_image:
            payload["excluded_fields"] = ["output_image"]
        start = time.monotonic()
        resp = requests.post(self.url, json=payload, timeout=cfg.WORKFLOW_TIMEOUT_SECONDS)
        rtt = time.monotonic() - start
        if resp.status_code >= 400:
            raise RuntimeError(f"workflow HTTP {resp.status_code}: {resp.text[:800]}")
        outputs = resp.json().get("outputs", [])
        if not outputs:
            raise RuntimeError("empty workflow response")
        return outputs[0], rtt


# --------------------------------------------------------------------------
# Client-side state machine (pure: facts + clock in, actions out -> testable)
# --------------------------------------------------------------------------
class LockboxStateMachine:
    """States: ARMED -> WAIT_OPEN -> UNLOCK_HOLD -> VERIFYING -> ARMED.

    WAIT_OPEN is the courier-courtesy delay: detection is confirmed, but the box
    opens PRE_OPEN_SECONDS later so the courier can read the sign and step over.
    The physical bolt is then held by the ESP32 for BOX_OPEN_SECONDS and closes
    on its own (firmware timer + thermal watchdog) - the client never has to
    "remember" to close it.

    step() never does I/O. It returns actions for the caller:
      "pulse"                                     - fire the lock (/open); caller must
                                                    call confirm_unlock() on success
      "event:delivery_confirmed"                  - terminal success
      "event:delivery_failed_package_on_ground"   - terminal failure
    """

    def __init__(self, package_in_box=False):
        self.state = "ARMED"
        self.unlock = False
        self.package_in_box = package_in_box
        self.dwell = 0
        self.misses = 0
        self.open_at = 0.0
        self.grace_end = 0.0
        self.extensions = 0
        self.verify_results = []
        self.verify_deadline = 0.0
        self.cooldown_until = 0.0

    def step(self, facts, now):
        actions = []
        pwp = bool(facts.get("person_with_package"))
        pkg = bool(facts.get("package_in_zone"))

        if self.state == "ARMED":
            self.unlock = False
            if now < self.cooldown_until:
                return actions
            if pwp:
                self.dwell += 1
                self.misses = 0
            elif self.dwell > 0:
                self.misses += 1
                if self.misses > cfg.DWELL_MISS_TOLERANCE:
                    self.dwell = 0
                    self.misses = 0
            if self.dwell >= cfg.DWELL_FRAMES:
                self.state = "WAIT_OPEN"
                self.open_at = now + cfg.PRE_OPEN_SECONDS
            return actions

        if self.state == "WAIT_OPEN":
            self.unlock = False
            if now >= self.open_at:
                actions.append("pulse")  # re-emitted next frame if the call fails
            return actions

        if self.state == "UNLOCK_HOLD":
            if now < self.grace_end:
                self.unlock = True  # latched: person/package may leave the frame freely
                return actions
            self.state = "VERIFYING"
            self.unlock = False
            self.verify_results = []
            self.verify_deadline = now + cfg.MAX_VERIFY_SECONDS
            # fall through: this frame is the first verification sample

        if self.state == "VERIFYING":
            self.unlock = False
            if pwp and self.extensions < cfg.MAX_GRACE_EXTENSIONS:
                # courier came back (or is still finishing) - restart the grace
                # window WITHOUT a new pulse; capped to prevent loiter-latching
                self.extensions += 1
                self.state = "UNLOCK_HOLD"
                self.unlock = True
                self.grace_end = now + cfg.GRACE_SECONDS
                return actions
            self.verify_results.append(pkg)
            if len(self.verify_results) >= cfg.VERIFY_FRAMES:
                majority = cfg.VERIFY_FRAMES // 2 + 1
                if sum(self.verify_results) >= majority:
                    actions.append("event:delivery_failed_package_on_ground")
                else:
                    self.package_in_box = True
                    actions.append("event:delivery_confirmed")
                self._reset(now)
        return actions

    def confirm_unlock(self, now):
        """Called by the owner after a successful ESP32 pulse."""
        self.state = "UNLOCK_HOLD"
        self.unlock = True
        self.grace_end = now + cfg.GRACE_SECONDS
        self.extensions = 0
        self.dwell = 0
        self.misses = 0

    def force_inconclusive(self, now):
        """Sustained network failure during VERIFYING: conservative outcome."""
        self.package_in_box = True  # user should check the box; recorded locally
        self._reset(now)

    def _reset(self, now):
        self.state = "ARMED"
        self.unlock = False
        self.dwell = 0
        self.misses = 0
        self.open_at = 0.0
        self.extensions = 0
        self.verify_results = []
        self.cooldown_until = now + cfg.EVENT_COOLDOWN_SECONDS

    def contract(self, facts, event=None):
        """The app-ready JSON a future consumer (Phase C UI) reads."""
        return {
            "unlock": self.unlock,
            "state": self.state,
            "package_in_zone": bool(facts.get("package_in_zone")),
            "person_in_zone": bool(facts.get("person_in_zone")),
            "person_with_package": bool(facts.get("person_with_package")),
            "package_in_box": self.package_in_box,
            "event": event,
            "raw": {
                "boxes": facts.get("boxes", []),
                "person_count": facts.get("person_count", 0),
                "package_count": facts.get("package_count", 0),
                "max_person_confidence": facts.get("max_person_confidence", 0.0),
                "max_package_confidence": facts.get("max_package_confidence", 0.0),
            },
        }


# --------------------------------------------------------------------------
# Persistence + event recording
# --------------------------------------------------------------------------
class PersistentState:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()  # saves happen from UI + notification threads
        self.data = {"package_in_box": False, "last_event": None, "last_event_time": None}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except Exception:
                log(f"WARNING: could not parse {self.path}, starting fresh")

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)


class EventRecorder:
    def __init__(self, events_dir):
        self.dir = Path(events_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def record(self, event, frame, facts, notified):
        import cv2
        stamp = time.strftime("%Y%m%d-%H%M%S")
        img_path = self.dir / f"{stamp}_{event}.jpg"
        if frame is not None:
            cv2.imwrite(str(img_path), frame)
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            "notified": notified,
            "image": img_path.name,
            "facts": {k: facts.get(k) for k in (
                "person_in_zone", "package_in_zone", "person_with_package",
                "person_count", "package_count",
                "max_person_confidence", "max_package_confidence")},
        }
        with open(self.dir / "events.jsonl", "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return img_path


# --------------------------------------------------------------------------
# ESP32
# --------------------------------------------------------------------------
def pulse_esp32(esp32_ip, path="/pulse"):
    """Call the lock. /open holds the bolt for the delivery window (firmware
    auto-closes it); /pulse is a 1 s manual test click. Never raises."""
    import requests
    if not esp32_ip:
        log("ERROR: ESP32_IP missing from .env - cannot reach the lock")
        return False
    url = f"http://{esp32_ip}{path}"
    for attempt in range(1 + cfg.ESP32_RETRIES):
        try:
            resp = requests.get(url, timeout=cfg.ESP32_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                return True
            log(f"ESP32 pulse HTTP {resp.status_code} (attempt {attempt + 1})")
        except Exception as exc:
            log(f"ESP32 pulse failed: {exc} (attempt {attempt + 1})")
    return False


# --------------------------------------------------------------------------
# Wake-word stand-in gates (ARMED state only)
# --------------------------------------------------------------------------
class MotionGate:
    """MOG2 background subtraction; opens for GATE_HOLD_SECONDS per trigger."""

    def __init__(self):
        import cv2
        self.subtractor = cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=32, detectShadows=False)
        self.open_until = 0.0

    def is_open(self, frame, now):
        import cv2
        small = cv2.resize(frame, (320, 240))
        mask = self.subtractor.apply(small)
        if (mask > 0).mean() >= cfg.MOTION_MIN_AREA_FRAC:
            self.open_until = now + cfg.GATE_HOLD_SECONDS
        return now < self.open_until


class HogGate:
    """HOG person detector. Weak at close range - the iPhone (Phase C) replaces
    this with a proper on-device person model."""

    def __init__(self):
        import cv2
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.open_until = 0.0

    def is_open(self, frame, now):
        import cv2
        scale = 640 / frame.shape[1]
        small = cv2.resize(frame, (640, max(1, int(frame.shape[0] * scale))))
        rects, _ = self.hog.detectMultiScale(small, winStride=(8, 8))
        if len(rects) > 0:
            self.open_until = now + cfg.GATE_HOLD_SECONDS
        return now < self.open_until


def make_gate(mode):
    if mode == "motion":
        return MotionGate()
    if mode == "hog":
        return HogGate()
    return None


# --------------------------------------------------------------------------
# Live loop
# --------------------------------------------------------------------------
def draw_preview(frame, zone, facts, sm, gated, now):
    """Overlay that explains itself: plain-English status, delivery zone,
    labeled detection boxes, and a key legend."""
    import cv2
    import numpy as np
    font = cv2.FONT_HERSHEY_SIMPLEX
    vis = frame.copy()
    height, width = vis.shape[:2]

    # delivery zone (tinted + outlined + named)
    zone_pts = np.array(zone, dtype=np.int32)
    overlay = vis.copy()
    cv2.fillPoly(overlay, [zone_pts], (0, 255, 255))
    vis = cv2.addWeighted(overlay, 0.12, vis, 0.88, 0)
    cv2.polylines(vis, [zone_pts], True, (0, 255, 255), 2)
    zone_x, zone_y = zone_pts.min(axis=0)
    cv2.putText(vis, "DELIVERY ZONE", (int(zone_x) + 8, int(zone_y) + 24),
                font, 0.65, (0, 255, 255), 2)

    # detection boxes with filled labels
    for det in (facts or {}).get("boxes", []):
        box = det["box"]
        pt1 = (int(box["x_min"]), int(box["y_min"]))
        pt2 = (int(box["x_max"]), int(box["y_max"]))
        color = (0, 140, 255) if det["class"] == "package" else (0, 220, 0)
        cv2.rectangle(vis, pt1, pt2, color, 3)
        label = f"{det['class'].upper()} {int(det['confidence'] * 100)}%"
        (tw, th), _ = cv2.getTextSize(label, font, 0.65, 2)
        ly = max(th + 12, pt1[1])
        cv2.rectangle(vis, (pt1[0], ly - th - 10), (pt1[0] + tw + 8, ly), color, -1)
        cv2.putText(vis, label, (pt1[0] + 4, ly - 5), font, 0.65, (0, 0, 0), 2)

    # plain-English status banner
    if sm.state == "ARMED":
        if gated:
            main, color = "IDLE - no motion, not streaming to the cloud", (190, 190, 190)
        elif sm.dwell > 0:
            main, color = (f"COURIER + PACKAGE SPOTTED - confirming {sm.dwell}/{cfg.DWELL_FRAMES} before unlocking",
                           (0, 255, 255))
        else:
            main, color = "WATCHING - waiting for a person WITH a package in the zone", (255, 255, 255)
    elif sm.state == "WAIT_OPEN":
        main, color = (f"CONFIRMED - box opens in {max(0, int(sm.open_at - now)) + 1}s (courier: see sign)",
                       (0, 255, 255))
    elif sm.state == "UNLOCK_HOLD":
        remaining = max(0, int(sm.grace_end - now))
        open_left = cfg.BOX_OPEN_SECONDS - (cfg.GRACE_SECONDS - remaining)
        if open_left > 0:
            main, color = f"BOX OPEN - place the package inside ({int(open_left)}s)", (0, 80, 255)
        else:
            main, color = f"DELIVERY IN PROGRESS - verifying in {remaining}s", (0, 165, 255)
    else:  # VERIFYING
        main, color = (f"VERIFYING - was the package put inside? (check {len(sm.verify_results)}/{cfg.VERIFY_FRAMES})",
                       (0, 165, 255))

    in_box = "YES (press 'e' after emptying)" if sm.package_in_box else "no"
    person = "YES" if (facts or {}).get("person_in_zone") else "no"
    package = "YES" if (facts or {}).get("package_in_zone") else "no"
    sub = f"person in zone: {person}    package in zone: {package}    package in lockbox: {in_box}"

    cv2.rectangle(vis, (0, 0), (width, 70), (25, 25, 25), -1)
    cv2.putText(vis, main, (10, 30), font, 0.8, color, 2)
    cv2.putText(vis, sub, (10, 58), font, 0.55, (215, 215, 215), 1)

    # loud red frame while the latch is held
    if sm.unlock:
        cv2.rectangle(vis, (0, 0), (width - 1, height - 1), (0, 80, 255), 10)

    cv2.rectangle(vis, (0, height - 30), (width, height), (25, 25, 25), -1)
    cv2.putText(vis, "keys:  q = quit    e = I emptied the box    u = unlock now (manual)",
                (10, height - 9), font, 0.55, (215, 215, 215), 1)
    cv2.imshow("lockbox", vis)


def emit_event(event, frame, facts, wf, zone, recorder, persist):
    """One event -> exactly one notification call (retried), one saved frame,
    one events.jsonl line, one persisted-state update."""
    notified = False
    for attempt in range(3):
        try:
            wf.infer(frame, zone, client_event=event)
            notified = True
            break
        except Exception as exc:
            log(f"event notification attempt {attempt + 1} failed: {exc}")
            time.sleep(2)
    img_path = recorder.record(event, frame, facts or {}, notified)
    persist.data["last_event"] = event
    persist.data["last_event_time"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    persist.save()
    log(f"*** EVENT {event} - notified={notified} frame={img_path}")
    return notified


def run_live(no_preview=False):
    import cv2
    from concurrent.futures import ThreadPoolExecutor

    api_key, esp32_ip = load_env()
    wf = WorkflowClient(api_key)
    persist = PersistentState(BASE_DIR / cfg.STATE_FILE)
    recorder = EventRecorder(BASE_DIR / cfg.EVENTS_DIR)
    sm = LockboxStateMachine(package_in_box=persist.data["package_in_box"])
    gate = make_gate(cfg.GATE_MODE)
    show_preview = cfg.SHOW_PREVIEW and not no_preview

    cap = cv2.VideoCapture(cfg.CAMERA_INDEX)
    if not cap.isOpened():
        raise SystemExit(f"cannot open camera index {cfg.CAMERA_INDEX}")
    ok, frame = cap.read()
    if not ok:
        raise SystemExit("camera opened but returned no frame")
    frame = prepare_frame(frame)
    height, width = frame.shape[:2]
    zone = resolve_zone(width, height)

    log(f"workflow: {wf.url}")
    log(f"model={cfg.MODEL_ID} zone={zone} gate={cfg.GATE_MODE} esp32={esp32_ip or 'MISSING'}")
    log(f"restored package_in_box={sm.package_in_box}  (press 'e' after emptying the box)")

    executor = ThreadPoolExecutor(max_workers=1)   # cloud inference calls
    io_pool = ThreadPoolExecutor(max_workers=2)    # ESP32 pulses + event notifications:
                                                   # NEVER on the UI thread (a 5s lock
                                                   # timeout would freeze the window)
    pending = None        # in-flight cloud call: (future, frame snapshot)
    pending_pulse = None  # in-flight ESP32 pulse: (future, frame snapshot, facts)
    next_sample = 0.0     # monotonic deadline for the next cloud sample
    last_pulse_attempt = 0.0
    last_facts = None
    last_gated = False
    failures = 0
    sample_ticks = 0
    frames_sent = 0  # vs sample_ticks: the wake-word cost-efficiency metric (Phase D)

    def handle_result(facts, frame_snap, rtt):
        nonlocal last_pulse_attempt, pending_pulse
        prev_state = sm.state
        actions = sm.step(facts, time.monotonic())

        for action in actions:
            if action == "pulse":
                if pending_pulse is None and time.monotonic() - last_pulse_attempt >= cfg.PULSE_RETRY_SECONDS:
                    last_pulse_attempt = time.monotonic()
                    log(f">>> opening the box ({cfg.BOX_OPEN_SECONDS}s delivery window) ...")
                    pending_pulse = (io_pool.submit(pulse_esp32, esp32_ip, cfg.UNLOCK_PATH), frame_snap, facts)
            elif action.startswith("event:"):
                event = action.split(":", 1)[1]

                def notify(event=event, snap=frame_snap, snap_facts=facts, in_box=sm.package_in_box):
                    emit_event(event, snap, snap_facts, wf, zone, recorder, persist)
                    persist.data["package_in_box"] = in_box
                    persist.save()

                io_pool.submit(notify)

        # optional re-pulse while holding, if the courier is back at the box
        if (cfg.REPULSE_ON_REAPPEAR and sm.state == "UNLOCK_HOLD"
                and facts.get("person_with_package")
                and time.monotonic() - last_pulse_attempt >= cfg.REPULSE_MIN_INTERVAL):
            last_pulse_attempt = time.monotonic()
            io_pool.submit(pulse_esp32, esp32_ip)

        if sm.state != prev_state:
            log(f">>> {prev_state} -> {sm.state}")

        log(f"{sm.state:<11} person={'Y' if facts.get('person_in_zone') else 'n'} "
            f"package={'Y' if facts.get('package_in_zone') else 'n'} "
            f"pwp={'Y' if facts.get('person_with_package') else 'n'} "
            f"dwell={min(sm.dwell, cfg.DWELL_FRAMES)}/{cfg.DWELL_FRAMES} unlock={sm.unlock} "
            f"in_box={sm.package_in_box} rtt={rtt * 1000:.0f}ms")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                log("ERROR: camera read failed, retrying in 2 s")
                time.sleep(2)
                continue
            frame = prepare_frame(frame)
            now = time.monotonic()

            if show_preview:
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("e"):
                    sm.package_in_box = False
                    persist.data["package_in_box"] = False
                    persist.save()
                    log(">>> box marked EMPTIED (package_in_box=false)")
                if key == ord("u"):
                    log(">>> manual pulse requested")
                    io_pool.submit(pulse_esp32, esp32_ip)

            # Collect a finished ESP32 pulse: only a SUCCESSFUL pulse starts
            # the unlock latch (the box must actually be open).
            if pending_pulse is not None and pending_pulse[0].done():
                pulse_future, pulse_snap, pulse_facts = pending_pulse
                pending_pulse = None
                pulsed_ok = False
                try:
                    pulsed_ok = pulse_future.result()
                except Exception as exc:
                    log(f"ERROR: pulse thread failed: {exc}")
                if pulsed_ok:
                    sm.confirm_unlock(time.monotonic())
                    recorder.record("unlock", pulse_snap, pulse_facts, notified=False)
                    log(f">>> BOX OPEN - bolt held {cfg.BOX_OPEN_SECONDS}s by the firmware; "
                        f"verification after {cfg.GRACE_SECONDS}s")
                else:
                    log("ERROR: lock call failed - will retry")

            # Collect the in-flight cloud call once it finishes (never blocks
            # the camera loop).
            if pending is not None and pending[0].done():
                future, snap = pending
                pending = None
                try:
                    facts, rtt = future.result()
                    failures = 0
                    last_facts = facts
                    handle_result(facts, snap, rtt)
                except Exception as exc:
                    failures += 1
                    log(f"ERROR: workflow call failed ({failures} in a row): {exc}")
                    if sm.state == "VERIFYING" and time.monotonic() > sm.verify_deadline:
                        log("*** verification INCONCLUSIVE (network) - assuming package in box; check it!")
                        sm.force_inconclusive(time.monotonic())
                        recorder.record("verification_inconclusive", snap, {}, notified=False)
                        persist.data["package_in_box"] = sm.package_in_box
                        persist.save()

            # Start the next cloud sample at ~TARGET_FPS (one at a time; if a
            # call runs long, the next tick waits). The wake-word gate applies
            # ONLY while ARMED - in UNLOCK_HOLD / VERIFYING frames must keep
            # flowing even if the porch goes motionless, otherwise the
            # grace-window verification frames would never be sent.
            if pending is None and now >= next_sample:
                next_sample = now + 1.0 / cfg.TARGET_FPS
                sample_ticks += 1
                last_gated = False
                if sm.state == "ARMED" and gate is not None:
                    last_gated = not gate.is_open(frame, now)
                if not last_gated:
                    frames_sent += 1
                    snap = frame.copy()
                    pending = (executor.submit(wf.infer, snap, zone), snap)

            # The preview repaints every camera frame (~30 fps) with the latest
            # known facts, so it looks smooth even though the cloud only sees
            # TARGET_FPS frames per second (boxes update ~once a second).
            if show_preview:
                draw_preview(frame, zone, last_facts, sm, gated=last_gated, now=time.monotonic())
    finally:
        executor.shutdown(wait=False)
        io_pool.shutdown(wait=False)
        cap.release()
        if show_preview:
            cv2.destroyAllWindows()
        if sample_ticks:
            saved = 100.0 * (1 - frames_sent / sample_ticks)
            log(f"session stats: {sample_ticks} sample ticks, {frames_sent} frames sent to cloud "
                f"({saved:.0f}% saved by the '{cfg.GATE_MODE}' gate)")


# --------------------------------------------------------------------------
# Single-shot mode (Phase A photo tests)
# --------------------------------------------------------------------------
def run_single_shot(image_path, event):
    import cv2

    api_key, _ = load_env()
    wf = WorkflowClient(api_key)
    frame = cv2.imread(image_path)
    if frame is None:
        raise SystemExit(f"cannot read image: {image_path}")
    frame = prepare_frame(frame)
    height, width = frame.shape[:2]
    zone = resolve_zone(width, height)

    facts, rtt = wf.infer(frame, zone, client_event=event or "none")
    facts.pop("output_image", None)
    print(f"# workflow response ({rtt * 1000:.0f} ms round trip, zone={zone})")
    print(json.dumps({k: v for k, v in facts.items() if k not in ("predictions", "zone_predictions")},
                     indent=2))
    sm = LockboxStateMachine()
    print("# app-contract view (state machine fields are client-owned)")
    print(json.dumps(sm.contract(facts, event=event), indent=2))
    if event:
        print(f"# event '{event}' sent: one Vision Event (the app's alert feed) + one dataset upload (retraining)")


# --------------------------------------------------------------------------
# Offline selftest of the state machine (no camera, no network, stdlib only)
# --------------------------------------------------------------------------
def run_selftest():
    results = []

    def facts(person=False, package=False):
        return {"person_in_zone": person, "package_in_zone": package,
                "person_with_package": person and package}

    def check(name, fn):
        try:
            fn()
            results.append((name, None))
            print(f"PASS  {name}")
        except AssertionError as exc:
            results.append((name, str(exc) or "assertion failed"))
            print(f"FAIL  {name}: {exc}")

    def drive_to_hold(sm, now):
        """dwell frames -> WAIT_OPEN -> pre-open delay elapses -> open -> confirmed."""
        for i in range(cfg.DWELL_FRAMES):
            actions = sm.step(facts(True, True), now + i)
        assert actions == [] and sm.state == "WAIT_OPEN", \
            f"expected WAIT_OPEN after dwell, got {sm.state} / {actions}"
        t = now + cfg.DWELL_FRAMES + cfg.PRE_OPEN_SECONDS
        actions = sm.step(facts(True, True), t)
        assert "pulse" in actions, f"expected open after the pre-open delay, got {actions}"
        sm.confirm_unlock(t)
        return t

    def t_no_unlock_without_package():
        sm = LockboxStateMachine()
        for i in range(10):
            assert sm.step(facts(person=True), i) == [], "person alone must never unlock"
        assert sm.state == "ARMED" and not sm.unlock

    def t_no_unlock_package_alone():
        sm = LockboxStateMachine()
        for i in range(10):
            assert sm.step(facts(package=True), i) == [], "package alone must never unlock"

    def t_happy_path():
        sm = LockboxStateMachine()
        now = drive_to_hold(sm, 0)
        assert sm.state == "UNLOCK_HOLD" and sm.unlock
        # mid-hold: courier walks out of view -> latch must hold
        sm.step(facts(), now + 5)
        assert sm.unlock, "latch must survive person/package leaving the frame"
        # grace expires; three clear frames -> delivery confirmed
        base = now + cfg.GRACE_SECONDS + 1
        events = []
        for i in range(cfg.VERIFY_FRAMES):
            events += sm.step(facts(), base + i)
        assert "event:delivery_confirmed" in events, f"got {events}"
        assert sm.package_in_box is True
        assert sm.state == "ARMED" and not sm.unlock

    def t_failure_path():
        sm = LockboxStateMachine()
        now = drive_to_hold(sm, 0)
        base = now + cfg.GRACE_SECONDS + 1
        events = []
        for i in range(cfg.VERIFY_FRAMES):
            events += sm.step(facts(package=True), base + i)
        assert "event:delivery_failed_package_on_ground" in events, f"got {events}"
        assert sm.package_in_box is False
        assert sm.state == "ARMED"

    def t_verify_majority():
        sm = LockboxStateMachine()
        now = drive_to_hold(sm, 0)
        base = now + cfg.GRACE_SECONDS + 1
        # one flicker frame with package, majority clear -> still confirmed
        seq = [facts(package=True)] + [facts()] * (cfg.VERIFY_FRAMES - 1)
        events = []
        for i, f in enumerate(seq):
            events += sm.step(f, base + i)
        assert "event:delivery_confirmed" in events, f"got {events}"

    def t_dwell_miss_tolerance():
        sm = LockboxStateMachine()
        seq = [facts(True, True), facts(True, False), facts(True, True), facts(True, True)]
        for i, f in enumerate(seq):
            sm.step(f, i)
        assert sm.state == "WAIT_OPEN", "one missed frame inside the streak must be tolerated"

    def t_dwell_reset():
        sm = LockboxStateMachine()
        seq = [facts(True, True), facts(), facts(), facts(True, True), facts(True, True)]
        actions = []
        for i, f in enumerate(seq):
            actions = sm.step(f, i)
        assert actions == [], "two misses must reset the dwell streak"
        assert sm.dwell == 2

    def t_pre_open_delay():
        sm = LockboxStateMachine()
        for i in range(cfg.DWELL_FRAMES):
            sm.step(facts(True, True), i)
        assert sm.state == "WAIT_OPEN" and not sm.unlock
        actions = sm.step(facts(True, True), sm.open_at - 0.5)
        assert actions == [], "must not open before the courier-courtesy delay elapses"

    def t_pulse_retry_when_esp32_down():
        sm = LockboxStateMachine()
        for i in range(cfg.DWELL_FRAMES):
            sm.step(facts(True, True), i)
        t = cfg.DWELL_FRAMES + cfg.PRE_OPEN_SECONDS
        actions = sm.step(facts(True, True), t)
        assert "pulse" in actions
        # owner does NOT confirm (lock call failed); next frame retries
        actions = sm.step(facts(True, True), t + 1)
        assert "pulse" in actions, "unconfirmed open must be re-emitted"
        assert sm.state == "WAIT_OPEN"

    def t_courier_return_extends_grace():
        sm = LockboxStateMachine()
        now = drive_to_hold(sm, 0)
        base = now + cfg.GRACE_SECONDS + 1
        events = sm.step(facts(True, True), base)  # reappears at verification time
        assert events == [] and sm.state == "UNLOCK_HOLD" and sm.unlock
        assert sm.extensions == 1
        # second grace expires cleanly -> confirmed
        base2 = base + cfg.GRACE_SECONDS + 1
        events = []
        for i in range(cfg.VERIFY_FRAMES):
            events += sm.step(facts(), base2 + i)
        assert "event:delivery_confirmed" in events

    def t_extension_cap():
        sm = LockboxStateMachine()
        now = drive_to_hold(sm, 0)
        t = now
        for _ in range(cfg.MAX_GRACE_EXTENSIONS):
            t += cfg.GRACE_SECONDS + 1
            sm.step(facts(True, True), t)
            assert sm.state == "UNLOCK_HOLD"
        # extensions exhausted: loitering with the package now counts as verify votes
        t += cfg.GRACE_SECONDS + 1
        events = []
        for i in range(cfg.VERIFY_FRAMES):
            events += sm.step(facts(True, True), t + i)
        assert "event:delivery_failed_package_on_ground" in events, f"got {events}"

    def t_cooldown_after_event():
        sm = LockboxStateMachine()
        now = drive_to_hold(sm, 0)
        base = now + cfg.GRACE_SECONDS + 1
        for i in range(cfg.VERIFY_FRAMES):
            sm.step(facts(), base + i)
        t = base + cfg.VERIFY_FRAMES
        for i in range(cfg.DWELL_FRAMES + 2):
            assert sm.step(facts(True, True), t + i) == [], "cooldown must suppress re-trigger"
        t2 = t + cfg.EVENT_COOLDOWN_SECONDS + 1
        for i in range(cfg.DWELL_FRAMES):
            sm.step(facts(True, True), t2 + i)
        assert sm.state == "WAIT_OPEN", "must re-arm after cooldown"
        actions = sm.step(facts(True, True), t2 + cfg.DWELL_FRAMES + cfg.PRE_OPEN_SECONDS)
        assert "pulse" in actions, "must open again after cooldown"

    def t_inconclusive():
        sm = LockboxStateMachine()
        now = drive_to_hold(sm, 0)
        sm.step(facts(), now + cfg.GRACE_SECONDS + 1)  # enter VERIFYING
        sm.force_inconclusive(now + cfg.GRACE_SECONDS + 2)
        assert sm.package_in_box is True and sm.state == "ARMED"

    check("no unlock: person without package", t_no_unlock_without_package)
    check("no unlock: package without person", t_no_unlock_package_alone)
    check("pre-open courtesy delay is honored", t_pre_open_delay)
    check("happy path: dwell -> pre-open wait -> open -> latch survives absence -> delivery_confirmed", t_happy_path)
    check("failure path: package still on ground -> delivery_failed", t_failure_path)
    check("verification is majority vote (flicker tolerated)", t_verify_majority)
    check("dwell tolerates a single missed frame", t_dwell_miss_tolerance)
    check("dwell resets after two missed frames", t_dwell_reset)
    check("pulse retries while ESP32 is down", t_pulse_retry_when_esp32_down)
    check("courier return during verification re-latches (extension)", t_courier_return_extends_grace)
    check("grace extensions are capped, then loitering fails delivery", t_extension_cap)
    check("post-event cooldown suppresses immediate re-trigger", t_cooldown_after_event)
    check("network-failure verification is conservative (package_in_box=true)", t_inconclusive)

    failed = [name for name, err in results if err]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", help="run once on a still image instead of the webcam")
    parser.add_argument("--event", choices=list(VALID_EVENTS),
                        help="with --image: tag the frame as this event (fires email/vision-event/upload)")
    parser.add_argument("--selftest", action="store_true", help="offline state-machine tests")
    parser.add_argument("--reset-box", action="store_true", help="clear the persisted package_in_box flag")
    parser.add_argument("--no-preview", action="store_true", help="disable the OpenCV preview window")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(run_selftest())
    if args.reset_box:
        persist = PersistentState(BASE_DIR / cfg.STATE_FILE)
        persist.data["package_in_box"] = False
        persist.save()
        print("package_in_box reset to false")
        return
    if args.image:
        run_single_shot(args.image, args.event)
        return
    run_live(no_preview=args.no_preview)


if __name__ == "__main__":
    main()

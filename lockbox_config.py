"""All tunables for the porch lockbox client. Secrets live in .env, never here.

Phase A (cloud) parameters are sent to the workflow on every request, so edits
here take effect immediately without redeploying the workflow.
"""

# ---------------------------------------------------------------- Roboflow --
ROBOFLOW_API_URL = "https://serverless.roboflow.com"
WORKSPACE = "aarnavs-space"
WORKFLOW_ID = "package-track"          # https://serverless.roboflow.com/infer/workflows/aarnavs-space/package-track
WORKFLOW_TIMEOUT_SECONDS = 15

# Detection (sent per request as workflow parameters)
MODEL_ID = "package-goilk-zcar8/1"     # project/version (NO workspace prefix); swappable, e.g. "bearbox/18" or "yolov8n-640"
RAW_CLASSES = ["0", "80"]              # class names the model emits BEFORE rename (0=person, 80=package)
MIN_CONFIDENCE = 0.4                   # model-level floor
PERSON_CONFIDENCE = 0.45               # per-class threshold (F1-optimal on the eval split)
PACKAGE_CONFIDENCE = 0.59
NOTIFY_EMAIL = "aarnav.shah@roboflow.com"

# Frames longer than this on their longest side get downscaled before use
# (12 MP iPhone photos -> ~1280px; cuts upload size and latency ~10x).
MAX_FRAME_DIM = 1280

# Porch zone polygon. Three accepted forms:
#   None              -> auto: central 70% of the frame
#   [[fx, fy], ...]   -> NORMALIZED 0-1 fractions of frame width/height (resolution-independent)
#   [[x, y], ...]     -> raw pixel coords of the (already downscaled) frames this client sends
#
# PORCH_ZONE below is your traced porch polygon (originally [[1617,2582],[884,2840],
# [9,3677],[20,4002],[2426,4024],[2382,3187],[2211,2692]] on the 3024x4032 iPhone
# photos), converted to normalized fractions so it works at any resolution.
# Top three vertices raised from your trace (0.64/0.704/0.668 -> 0.55/0.60/0.55):
# your polygon covered where packages LAND, but the unlock condition also needs
# the standing courier inside the zone, and their body center sits at y~0.60.
# Lower the 0.55s if couriers stand further back; raise them if the street
# starts triggering detections.
PORCH_ZONE = [
    [0.535, 0.550],
    [0.292, 0.600],
    [0.003, 0.912],
    [0.007, 0.993],
    [0.802, 0.998],
    [0.788, 0.790],
    [0.731, 0.550],
]
# Webcam milestone testing: None = central 70% of the webcam frame.
# Switch back to ZONE = PORCH_ZONE when testing porch photos / the real camera.
ZONE = None

# ------------------------------------------------------------------ Camera --
CAMERA_INDEX = 0
TARGET_FPS = 1.0                       # cloud samples per second; the preview always renders
                                       # at full camera speed (~30 fps) regardless
SHOW_PREVIEW = True                    # OpenCV window with zone, boxes, state banner

# Wake-word stand-in gate (ARMED state only; other states always stream)
GATE_MODE = "motion"                   # "off" | "motion" (MOG2) | "hog" (poor at close range)
GATE_HOLD_SECONDS = 10.0               # keep streaming this long after the last trigger
MOTION_MIN_AREA_FRAC = 0.02            # fraction of pixels that must change to trigger

# ----------------------------------------------------------- State machine --
DWELL_FRAMES = 3                       # consecutive person-with-package frames to confirm (~2-3 s at 1 fps)
PRE_OPEN_SECONDS = 5.0                 # after confirmation, wait this long before opening
                                       # (the courier reads the sign and steps to the box)
BOX_OPEN_SECONDS = 13                  # how long the bolt is physically held open - MUST
                                       # match OPEN_HOLD_MS in esp32_lockbox.ino (13000 ms)
DWELL_MISS_TOLERANCE = 1               # missed frames tolerated inside a dwell streak
GRACE_SECONDS = 15                     # TESTING value - "delivery in progress" window
                                       # before verification. Must stay > BOX_OPEN_SECONDS
                                       # (13). Restore to ~90 for real porch use.
VERIFY_FRAMES = 3                      # frames voted after the grace window (majority wins)
MAX_GRACE_EXTENSIONS = 2               # courier reappearing with package restarts grace at most this many times
MAX_VERIFY_SECONDS = 120               # verification watchdog (network-failure fallback)
EVENT_COOLDOWN_SECONDS = 60            # suppress re-trigger after a terminal event

# ------------------------------------------------------------------- ESP32 --
# ESP32_IP comes from .env. /open holds the bolt for the delivery window;
# /pulse (manual 'u' key) is a 1 s test click.
UNLOCK_PATH = "/open"
ESP32_TIMEOUT_SECONDS = 5
ESP32_RETRIES = 2                      # extra attempts per pulse decision
PULSE_RETRY_SECONDS = 5.0              # min seconds between pulse attempts while dwell stays satisfied
REPULSE_ON_REAPPEAR = False            # optional: re-pulse when courier+package reappear mid-hold
REPULSE_MIN_INTERVAL = 10.0

# ------------------------------------------------------------------- Paths --
STATE_FILE = "state.json"              # persists package_in_box across restarts
EVENTS_DIR = "events"                  # triggering frames + events.jsonl

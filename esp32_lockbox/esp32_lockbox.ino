/*
 * Porch Lockbox — ESP32 firmware
 *
 * Serves the HTTP interface the vision client expects:
 *   GET /pulse   -> energize relay for 1 s (bolt retracts), then release (spring re-locks)
 *   GET /toggle  -> manual unlock/lock; auto-relocks after 30 s as a safety net
 *   GET /status  -> JSON state (for the future app)
 *   GET /        -> phone-friendly page with Unlock buttons
 *
 * Also advertises itself as http://lockbox.local/ (mDNS) so you never
 * have to hunt for its IP address.
 *
 * Wi-Fi credentials live in credentials.h (gitignored) - copy
 * credentials.example.h to credentials.h and fill it in.
 */
#include "credentials.h"

const int   RELAY_PIN     = 4;    // the GPIO your relay's IN wire is on

// This board is a High/Low Level Trigger module with a selection jumper.
// The jumper is on H (high trigger), so the firmware must be active-HIGH: false.
// RULE: this constant and the yellow jumper must agree - if you ever move the
// jumper to L, flip this to true. Verify after any change: at idle only the
// green PWR LED is lit and the solenoid is cold.
const bool RELAY_ACTIVE_LOW = false;

const unsigned long PULSE_MS       = 1000;   // /pulse: bolt retraction time
const unsigned long OPEN_HOLD_MS   = 13000;  // /open: delivery window - bolt held
                                             // open while the courier loads the box
                                             // (must match BOX_OPEN_SECONDS in
                                             // lockbox_config.py)
const unsigned long TOGGLE_RELOCK_MS = 30000; // /toggle: safety auto-relock

// SOLENOID THERMAL PROTECTION: lock solenoids are pulse-duty parts and overheat
// if held on. No matter what state or bug occurs, the coil is force-released
// after this much continuous on-time.
const unsigned long MAX_COIL_ON_MS = 30000;

#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>

WebServer server(80);
bool unlocked = false;                 // current /toggle state
unsigned long relockAt = 0;            // when to auto-relock (0 = no timer)
unsigned long coilOnSince = 0;         // 0 = coil released

void relay(bool on) {
  digitalWrite(RELAY_PIN, (on ^ !RELAY_ACTIVE_LOW) ? LOW : HIGH);
  coilOnSince = on ? millis() : 0;
}

void handlePulse() {
  relay(true);
  delay(PULSE_MS);
  relay(false);
  unlocked = false;
  relockAt = 0;
  server.send(200, "text/plain", "pulsed");
  Serial.println("[pulse] bolt retracted 1s, re-locked");
}

void handleOpen() {
  unlocked = true;
  relay(true);
  relockAt = millis() + OPEN_HOLD_MS;
  server.send(200, "text/plain", "open (auto-lock in 13s)");
  Serial.println("[open] delivery window: bolt held 13s");
}

void handleToggle() {
  unlocked = !unlocked;
  relay(unlocked);
  relockAt = unlocked ? millis() + TOGGLE_RELOCK_MS : 0;
  server.send(200, "text/plain", unlocked ? "unlocked (auto-relock in 30s)" : "locked");
  Serial.println(unlocked ? "[toggle] UNLOCKED, 30s timer" : "[toggle] LOCKED");
}

void handleStatus() {
  String json = "{\"unlocked\":";
  json += unlocked ? "true" : "false";
  json += ",\"auto_relock_ms\":";
  json += (relockAt > millis()) ? String(relockAt - millis()) : "0";
  json += "}";
  server.send(200, "application/json", json);
}

void handleRoot() {
  server.send(200, "text/html",
    "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<style>body{font-family:sans-serif;text-align:center;padding-top:3em}"
    "a{display:block;margin:1em auto;padding:1.2em;max-width:280px;border-radius:12px;"
    "background:#223;color:#fff;text-decoration:none;font-size:1.2em}</style>"
    "<h2>Porch Lockbox</h2>"
    "<a href='/open'>Open for delivery (13s)</a>"
    "<a href='/pulse'>Pulse (1s unlock)</a>"
    "<a href='/toggle'>Toggle (30s auto-relock)</a>"
    "<a href='/status'>Status</a>");
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  relay(false);  // locked (fail-secure): relay released at boot

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("Connected. IP address: ");
  Serial.println(WiFi.localIP());   // <-- put this in the vision client's .env

  if (MDNS.begin("lockbox")) {
    Serial.println("mDNS ready: http://lockbox.local/");
  }

  server.on("/", handleRoot);
  server.on("/open", handleOpen);
  server.on("/pulse", handlePulse);
  server.on("/toggle", handleToggle);
  server.on("/status", handleStatus);
  server.begin();
  Serial.println("HTTP server started");
}

void loop() {
  server.handleClient();
  if (relockAt != 0 && millis() > relockAt) {   // /toggle safety timer
    unlocked = false;
    relay(false);
    relockAt = 0;
    Serial.println("[auto] 30s elapsed, re-locked");
  }
  // thermal watchdog: never allow continuous coil current beyond the cap
  if (coilOnSince != 0 && millis() - coilOnSince > MAX_COIL_ON_MS) {
    unlocked = false;
    relay(false);
    relockAt = 0;
    Serial.println("[SAFETY] coil on too long - forced release to protect the solenoid");
  }
}
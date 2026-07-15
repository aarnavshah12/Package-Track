//
//  LockboxConfig.swift
//  All tunables for the porch lockbox. Secrets live in LockboxSecrets.swift.
//  Values mirror lockbox_config.py in the Package-Track repo - keep them in sync.
//

import Foundation

enum LockboxConfig {

    // ------------------------------------------------------------- Cloud --
    static let workflowURL = URL(string: "https://serverless.roboflow.com/infer/workflows/aarnavs-space/package-track")!
    static let workflowTimeout: TimeInterval = 15

    // Detection parameters sent with every workflow request
    static let modelId = "package-goilk-zcar8/1"
    static let rawClasses = ["0", "80"]          // model's numeric class names pre-rename
    static let minConfidence = 0.4
    static let personConfidence = 0.45
    static let packageConfidence = 0.59

    // Porch zone, normalized 0-1 (same polygon as PORCH_ZONE in lockbox_config.py).
    // Scaled to the sent frame's pixel size at request time.
    static let zoneNormalized: [[Double]] = [
        [0.535, 0.550], [0.292, 0.600], [0.003, 0.912], [0.007, 0.993],
        [0.802, 0.998], [0.788, 0.790], [0.731, 0.550],
    ]

    static func zonePixels(width: Int, height: Int) -> [[Int]] {
        zoneNormalized.map { [Int(($0[0] * Double(width)).rounded()), Int(($0[1] * Double(height)).rounded())] }
    }

    // ----------------------------------------------- On-device wake gate --
    // Class names the on-device model emits
    static let personClass = "0"
    static let packageClass = "80"
    static let vehicleClasses: Set<String> = ["2", "7"]   // car, truck (COCO leftovers - useful!)

    static let wakePersonFrames = 2                 // person alone must persist this many frames
    static let sleepAfterQuietSeconds: TimeInterval = 30
    static let streamFPS = 1.0                      // cloud sampling rate while awake

    // ------------------------------------------------------ State machine --
    static let dwellFrames = 3                      // cloud-confirmed person+package frames to confirm
    static let dwellMissTolerance = 1
    static let preOpenSeconds: TimeInterval = 5     // courier reads the sign
    static let boxOpenSeconds = 13                  // must match OPEN_HOLD_MS in esp32_lockbox.ino
    static let graceSeconds: TimeInterval = 15      // TESTING value; ~90 for real porch use
    static let verifyFrames = 3                     // majority vote
    static let maxGraceExtensions = 2
    static let maxVerifySeconds: TimeInterval = 120
    static let eventCooldownSeconds: TimeInterval = 60

    // --------------------------------------------------------------- Lock --
    static let unlockPath = "/open"                 // held-open delivery window (auto-closes)
    static let manualPulsePath = "/pulse"           // 1s manual test click
    static let esp32Timeout: TimeInterval = 5
    static let esp32Retries = 2                     // extra in-decision attempts (Python parity)
    static let openRetrySeconds: TimeInterval = 5
    static let eventNotifyAttempts = 3              // terminal-event notification retries
    static let eventNotifyRetryDelay: TimeInterval = 2
}

//
//  LockboxBrain.swift
//  Client-side delivery state machine - a 1:1 port of LockboxStateMachine in
//  lockbox_client.py (which passes a 13-scenario selftest). Pure logic: facts
//  and a clock in, actions out. No I/O in here, ever.
//
//  ARMED -> WAIT_OPEN -> UNLOCK_HOLD -> VERIFYING -> ARMED
//
//  WAIT_OPEN is the courier-courtesy delay (read the sign, step to the box).
//  The physical bolt is held by the ESP32 for boxOpenSeconds and closes on its
//  own firmware timer - this class never has to "remember" to close it.
//

import Foundation

enum LockboxState: String {
    case armed = "ARMED"
    case waitOpen = "WAIT_OPEN"
    case unlockHold = "UNLOCK_HOLD"
    case verifying = "VERIFYING"
}

enum LockboxAction: Equatable {
    case openLock                 // fire /open; caller must call confirmUnlock() on success
    case emitEvent(String)        // "delivery_confirmed" | "delivery_failed_package_on_ground"
}

struct FrameFacts {
    var personInZone = false
    var packageInZone = false
    var personWithPackage = false
    var personCount = 0
    var packageCount = 0
    var maxPersonConfidence = 0.0
    var maxPackageConfidence = 0.0
}

final class LockboxBrain {
    private(set) var state: LockboxState = .armed
    private(set) var unlock = false
    var packageInBox: Bool

    private(set) var dwell = 0
    private var misses = 0
    private(set) var openAt: TimeInterval = 0
    private(set) var graceEnd: TimeInterval = 0
    private(set) var extensions = 0
    private(set) var verifyResults: [Bool] = []
    private(set) var verifyDeadline: TimeInterval = 0
    private(set) var cooldownUntil: TimeInterval = 0

    init(packageInBox: Bool = false) {
        self.packageInBox = packageInBox
    }

    func step(_ facts: FrameFacts, now: TimeInterval) -> [LockboxAction] {
        var actions: [LockboxAction] = []
        let pwp = facts.personWithPackage
        let pkg = facts.packageInZone

        switch state {
        case .armed:
            unlock = false
            if now < cooldownUntil { return actions }
            if pwp {
                dwell += 1
                misses = 0
            } else if dwell > 0 {
                misses += 1
                if misses > LockboxConfig.dwellMissTolerance {
                    dwell = 0
                    misses = 0
                }
            }
            if dwell >= LockboxConfig.dwellFrames {
                state = .waitOpen
                openAt = now + LockboxConfig.preOpenSeconds
            }
            return actions

        case .waitOpen:
            unlock = false
            if now >= openAt {
                actions.append(.openLock)   // re-emitted next frame if the call fails
            }
            return actions

        case .unlockHold:
            if now < graceEnd {
                unlock = true               // latched: courier may leave the frame freely
                return actions
            }
            state = .verifying
            unlock = false
            verifyResults = []
            verifyDeadline = now + LockboxConfig.maxVerifySeconds
            fallthrough                     // this frame is the first verification sample

        case .verifying:
            unlock = false
            if pwp && extensions < LockboxConfig.maxGraceExtensions {
                // courier came back (or is still finishing) - restart the grace
                // window WITHOUT a new open; capped to prevent loiter-latching
                extensions += 1
                state = .unlockHold
                unlock = true
                graceEnd = now + LockboxConfig.graceSeconds
                return actions
            }
            verifyResults.append(pkg)
            if verifyResults.count >= LockboxConfig.verifyFrames {
                let majority = LockboxConfig.verifyFrames / 2 + 1
                if verifyResults.filter({ $0 }).count >= majority {
                    actions.append(.emitEvent("delivery_failed_package_on_ground"))
                } else {
                    packageInBox = true
                    actions.append(.emitEvent("delivery_confirmed"))
                }
                reset(now: now)
            }
            return actions
        }
    }

    /// Called by the owner after the ESP32 /open call actually succeeded.
    func confirmUnlock(now: TimeInterval) {
        state = .unlockHold
        unlock = true
        graceEnd = now + LockboxConfig.graceSeconds
        extensions = 0
        dwell = 0
        misses = 0
    }

    /// Sustained network failure during VERIFYING: conservative outcome.
    func forceInconclusive(now: TimeInterval) {
        packageInBox = true   // user should check the box
        reset(now: now)
    }

    /// App returned from suspension mid-delivery: uptime-based timers are
    /// untrustworthy across sleep. Safe to abort - the bolt self-closes on
    /// the ESP32's own firmware timer regardless of what the phone does.
    func abortToArmed(now: TimeInterval) {
        reset(now: now)
    }

    private func reset(now: TimeInterval) {
        state = .armed
        unlock = false
        dwell = 0
        misses = 0
        openAt = 0
        extensions = 0
        verifyResults = []
        cooldownUntil = now + LockboxConfig.eventCooldownSeconds
    }
}

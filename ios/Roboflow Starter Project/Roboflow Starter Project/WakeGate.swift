//
//  WakeGate.swift
//  The wake-word tier system. Decides only WHEN frames stream to the cloud -
//  never whether the box unlocks (that bar lives in the cloud facts + brain).
//
//  Tier 1: vehicle (car/truck) seen once           -> stream instantly
//  Tier 2: person + package seen together once     -> stream instantly
//  Tier 3: person alone for wakePersonFrames       -> stream (on-foot couriers)
//  Sleep:  nothing relevant for sleepAfterQuiet    -> stop (unless delivery in progress)
//

import Foundation

final class WakeGate {
    private(set) var streaming = false
    private(set) var reason = ""

    private var personStreak = 0
    private var vehicleWasPresent = false
    private var lastActivity: TimeInterval = 0

    func update(personSeen: Bool, packageSeen: Bool, vehicleSeen: Bool,
                deliveryInProgress: Bool, now: TimeInterval) {
        personStreak = personSeen ? personStreak + 1 : 0
        let personWake = personStreak >= LockboxConfig.wakePersonFrames

        // Vehicles wake on ARRIVAL (edge), not presence - otherwise a parked
        // car in view would keep the stream open forever and kill the whole
        // cost-saving story. People DO refresh on presence: a loitering human
        // is worth watching; a parked car is furniture.
        let vehicleArrived = vehicleSeen && !vehicleWasPresent
        vehicleWasPresent = vehicleSeen

        if vehicleArrived || (personSeen && packageSeen) || personWake || deliveryInProgress {
            lastActivity = now
        }

        if deliveryInProgress {
            streaming = true
            reason = "delivery in progress"
            return
        }

        if !streaming {
            if vehicleArrived {
                streaming = true
                reason = "vehicle arrived"
            } else if personSeen && packageSeen {
                streaming = true
                reason = "person + package"
            } else if personWake {
                streaming = true
                reason = "person present"
            }
        } else if now - lastActivity > LockboxConfig.sleepAfterQuietSeconds {
            streaming = false
            reason = ""
        }
    }
}

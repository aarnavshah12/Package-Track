//
//  LockboxController.swift
//  Glue between the camera (on-device detections), the wake gate, the cloud
//  workflow, the brain, and the lock. All state lives on the main queue.
//

import UIKit
import Roboflow

final class LockboxController {

    let brain = LockboxBrain(packageInBox: UserDefaults.standard.bool(forKey: "package_in_box"))
    let gate = WakeGate()
    private let workflow = WorkflowClient()
    private let lock = ESP32Client()

    private var pendingCloudCall = false
    private var pendingOpen = false
    private var nextSampleAt: TimeInterval = 0
    private var lastOpenAttempt: TimeInterval = 0
    private(set) var lastFacts: FrameFacts?
    private(set) var lastEvent = "none"

    // Wake-word cost metrics (for the writeup)
    private(set) var framesSeen = 0
    private(set) var framesStreamed = 0

    /// (headline, detail) - the ViewController renders these.
    var onStatusChanged: ((String, String) -> Void)?

    // Dashboard live-view mirroring: ~8 fps while someone is watching,
    // a trickle heartbeat otherwise. Commands queued on the dashboard are
    // executed here - this phone is the household's hand on the LAN.
    private var viewersWatching = false
    private var nextMirrorAt: TimeInterval = 0
    private var nextCtlAt: TimeInterval = 0

    private var now: TimeInterval { ProcessInfo.processInfo.systemUptime }

    init() {
        // systemUptime pauses during deep sleep, so timers can't be trusted
        // across a suspension. If the app comes back mid-delivery, abort to
        // ARMED (the bolt already self-closed on the firmware timer).
        NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self = self, self.brain.state != .armed else { return }
            print("[lockbox] resumed from suspension mid-delivery - resetting to ARMED")
            self.brain.abortToArmed(now: self.now)
            self.publishStatus()
        }
    }

    // ------------------------------------------------------------------
    // Called for every on-device detection pass (background queue).
    // imageProvider is only invoked when a frame is actually streamed,
    // so the 25fps gate path costs no image conversions.
    // ------------------------------------------------------------------
    func processFrame(detections: [RFObjectDetectionPrediction], imageProvider: @escaping () -> UIImage?) {
        DispatchQueue.main.async { [self] in
            handleFrame(detections: detections, imageProvider: imageProvider)
        }
    }

    private func handleFrame(detections: [RFObjectDetectionPrediction], imageProvider: () -> UIImage?) {
        framesSeen += 1
        let classes = Set(detections.map { $0.className })
        let personSeen = classes.contains(LockboxConfig.personClass)
        let packageSeen = classes.contains(LockboxConfig.packageClass)
        let vehicleSeen = !classes.isDisjoint(with: LockboxConfig.vehicleClasses)

        gate.update(personSeen: personSeen,
                    packageSeen: packageSeen,
                    vehicleSeen: vehicleSeen,
                    deliveryInProgress: brain.state != .armed,
                    now: now)

        mirrorTick(imageProvider: imageProvider)
        controlTick()

        if gate.streaming && !pendingCloudCall && now >= nextSampleAt, let image = imageProvider() {
            nextSampleAt = now + 1.0 / LockboxConfig.streamFPS
            sample(image: image)
        }

        publishStatus()
    }

    // ------------------------------------------------------------------
    // Dashboard mirroring + remote commands
    // ------------------------------------------------------------------
    private func mirrorTick(imageProvider: () -> UIImage?) {
        let fps = viewersWatching ? 8.0 : 0.05   // 8 fps watched, 1 frame / 20 s idle
        guard now >= nextMirrorAt, let image = imageProvider() else { return }
        nextMirrorAt = now + 1.0 / fps
        DispatchQueue.global(qos: .utility).async {
            let small = image.resizedToMaxDimension(640)
            if let jpeg = small.jpegData(compressionQuality: 0.5) {
                DashboardClient.shared.postSnapshot(jpeg)
            }
        }
    }

    private func controlTick() {
        guard now >= nextCtlAt else { return }
        nextCtlAt = now + 3
        DashboardClient.shared.fetchStreamCtl { [weak self] ctl in
            guard let self = self, let ctl = ctl else { return }
            self.viewersWatching = ctl.viewers > 0
            for cmd in ctl.commands { self.executeRemote(cmd) }
        }
    }

    private func executeRemote(_ cmd: String) {
        print("[lockbox] remote command from dashboard: \(cmd)")
        switch cmd {
        case "open": lock.openForDelivery { _ in }
        case "pulse": lock.manualPulse()
        case "box_emptied": boxEmptied()
        default: break
        }
    }

    // ------------------------------------------------------------------
    // Cloud sampling + brain stepping
    // ------------------------------------------------------------------
    private func sample(image: UIImage) {
        pendingCloudCall = true
        framesStreamed += 1
        workflow.infer(image: image) { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.pendingCloudCall = false
                switch result {
                case .success(let facts):
                    self.lastFacts = facts
                    self.act(on: self.brain.step(facts, now: self.now), image: image, facts: facts)
                case .failure(let error):
                    print("[lockbox] cloud call failed: \(error)")
                    if self.brain.state == .verifying && self.now > self.brain.verifyDeadline {
                        print("[lockbox] verification INCONCLUSIVE (network) - assuming package in box")
                        self.brain.forceInconclusive(now: self.now)
                        self.persistPackageInBox()
                    }
                }
                self.publishStatus()
            }
        }
    }

    private func act(on actions: [LockboxAction], image: UIImage, facts: FrameFacts) {
        for action in actions {
            switch action {
            case .openLock:
                guard !pendingOpen, now - lastOpenAttempt >= LockboxConfig.openRetrySeconds else { break }
                lastOpenAttempt = now
                pendingOpen = true
                print("[lockbox] >>> opening the box (\(LockboxConfig.boxOpenSeconds)s delivery window)")
                lock.openForDelivery { [weak self] ok in
                    guard let self = self else { return }
                    self.pendingOpen = false
                    if ok {
                        self.brain.confirmUnlock(now: self.now)
                        print("[lockbox] >>> BOX OPEN")
                        EventStore.shared.record(event: "box_opened", image: image)
                        DashboardClient.shared.postEvent("box_opened", jpeg: image.eventJPEG())
                    } else {
                        print("[lockbox] ERROR: lock unreachable - will retry")
                    }
                    self.publishStatus()
                }

            case .emitEvent(let event):
                lastEvent = event
                print("[lockbox] *** EVENT \(event)")
                EventStore.shared.record(event: event, image: image)
                DashboardClient.shared.postEvent(event, jpeg: image.eventJPEG())
                persistPackageInBox()
                // Re-send the deciding frame tagged with the event: fires the
                // vision event + dataset upload server-side, exactly once.
                // Retried like the Python client so a Wi-Fi blip can't
                // permanently lose the delivery record.
                sendEventNotification(event: event, image: image, attempt: 1)
            }
        }
    }

    private func sendEventNotification(event: String, image: UIImage, attempt: Int) {
        workflow.infer(image: image, clientEvent: event) { [weak self] result in
            DispatchQueue.main.async {
                switch result {
                case .success:
                    print("[lockbox] event \(event) recorded (attempt \(attempt))")
                case .failure(let error):
                    print("[lockbox] event notification attempt \(attempt) failed: \(error)")
                    if attempt < LockboxConfig.eventNotifyAttempts {
                        DispatchQueue.main.asyncAfter(deadline: .now() + LockboxConfig.eventNotifyRetryDelay) {
                            self?.sendEventNotification(event: event, image: image, attempt: attempt + 1)
                        }
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // User actions (buttons)
    // ------------------------------------------------------------------
    func boxEmptied() {
        brain.packageInBox = false
        persistPackageInBox()
        publishStatus()
    }

    func manualUnlock() {
        print("[lockbox] manual pulse requested")
        lock.manualPulse()
    }

    private func persistPackageInBox() {
        UserDefaults.standard.set(brain.packageInBox, forKey: "package_in_box")
    }

    // ------------------------------------------------------------------
    // Status text (mirrors the Mac client's plain-English overlay)
    // ------------------------------------------------------------------
    private func publishStatus() {
        let headline: String
        switch brain.state {
        case .armed:
            if !gate.streaming {
                headline = "IDLE - watching on-device only"
            } else if brain.dwell > 0 {
                headline = "COURIER + PACKAGE SPOTTED - confirming \(min(brain.dwell, LockboxConfig.dwellFrames))/\(LockboxConfig.dwellFrames)"
            } else {
                headline = "STREAMING (\(gate.reason)) - waiting for person + package in zone"
            }
        case .waitOpen:
            let secs = max(0, Int(brain.openAt - now)) + 1
            headline = "CONFIRMED - box opens in \(secs)s (courier: see sign)"
        case .unlockHold:
            let remaining = max(0, Int(brain.graceEnd - now))
            let openLeft = LockboxConfig.boxOpenSeconds - (Int(LockboxConfig.graceSeconds) - remaining)
            headline = openLeft > 0
                ? "BOX OPEN - place the package inside (\(openLeft)s)"
                : "DELIVERY IN PROGRESS - verifying in \(remaining)s"
        case .verifying:
            headline = "VERIFYING - was the package put inside? (\(brain.verifyResults.count)/\(LockboxConfig.verifyFrames))"
        }

        let person = lastFacts?.personInZone == true ? "YES" : "no"
        let package = lastFacts?.packageInZone == true ? "YES" : "no"
        let inBox = brain.packageInBox ? "YES" : "no"
        let saved = framesSeen > 0 ? 100 - Int(100.0 * Double(framesStreamed) / Double(framesSeen)) : 100
        let detail = "cloud: person \(person) · package \(package) · in box: \(inBox) · \(saved)% frames saved"

        onStatusChanged?(headline, detail)
    }
}

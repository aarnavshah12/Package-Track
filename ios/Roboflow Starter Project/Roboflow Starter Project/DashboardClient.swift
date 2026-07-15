//
//  DashboardClient.swift
//  Mirrors the camera's cloud-bound frames and event photos to the household
//  dashboard server on the LAN (the Mac). Strictly best-effort: failures are
//  silent and never affect the lockbox brain.
//

import Foundation

final class DashboardClient {
    static let shared = DashboardClient()

    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 4
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg)
    }()

    private var base: String? {
        LockboxSecrets.dashboardHost.isEmpty ? nil : "http://\(LockboxSecrets.dashboardHost)"
    }

    /// Called with every frame that streams to the cloud (~1/s while awake).
    func postSnapshot(_ jpeg: Data) {
        guard let base = base, let url = URL(string: "\(base)/api/snapshot") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.httpBody = jpeg
        session.dataTask(with: request).resume()
    }

    /// Called once per lockbox event with the deciding frame.
    func postEvent(_ event: String, jpeg: Data?) {
        guard let base = base, let url = URL(string: "\(base)/api/event") else { return }
        var payload: [String: Any] = ["event": event]
        if let jpeg = jpeg {
            payload["image_b64"] = jpeg.base64EncodedString()
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        session.dataTask(with: request).resume()
    }
}

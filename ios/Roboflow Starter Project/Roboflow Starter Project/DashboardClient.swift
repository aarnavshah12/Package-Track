//
//  DashboardClient.swift
//  Talks to the household dashboard server (Mac on the LAN today, cloud host
//  tomorrow - set dashboardHost to either "name.local:8321" or a full
//  "https://..." URL). Strictly best-effort: failures are silent and never
//  affect the lockbox brain.
//

import Foundation

final class DashboardClient {
    static let shared = DashboardClient()

    struct StreamCtl: Decodable {
        let viewers: Int
        let commands: [String]
    }

    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 6
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg)
    }()

    private var base: String? {
        let host = LockboxSecrets.dashboardHost
        if host.isEmpty { return nil }
        return host.contains("://") ? host : "http://\(host)"
    }

    private func request(_ path: String, method: String = "GET") -> URLRequest? {
        guard let base = base, let url = URL(string: base + path) else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue(LockboxSecrets.dashboardToken, forHTTPHeaderField: "X-Lockbox-Token")
        return request
    }

    /// Live-view mirror frame. Rate is decided by the controller (viewer-aware).
    func postSnapshot(_ jpeg: Data) {
        guard var request = request("/api/snapshot", method: "POST") else { return }
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.httpBody = jpeg
        session.dataTask(with: request).resume()
    }

    /// One per lockbox event, with the deciding frame.
    func postEvent(_ event: String, jpeg: Data?) {
        guard var request = request("/api/event", method: "POST") else { return }
        var payload: [String: Any] = ["event": event]
        if let jpeg = jpeg { payload["image_b64"] = jpeg.base64EncodedString() }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        session.dataTask(with: request).resume()
    }

    /// Heartbeat: is anyone watching (raise mirror FPS) + any queued commands
    /// from the dashboard for this phone to execute on the LAN.
    func fetchStreamCtl(completion: @escaping (StreamCtl?) -> Void) {
        guard let request = request("/api/streamctl") else { return completion(nil) }
        session.dataTask(with: request) { data, _, _ in
            let ctl = data.flatMap { try? JSONDecoder().decode(StreamCtl.self, from: $0) }
            DispatchQueue.main.async { completion(ctl) }
        }.resume()
    }
}

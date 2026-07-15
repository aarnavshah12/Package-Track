//
//  ESP32Client.swift
//  Talks to the lock over the LAN. The cloud never reaches the lock - this
//  phone is the only bridge, exactly like the Mac client was in Phase B.
//

import Foundation

final class ESP32Client {

    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = LockboxConfig.esp32Timeout
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg)
    }()

    /// Open the delivery window (/open, held ~13s, firmware auto-closes).
    func openForDelivery(completion: @escaping (Bool) -> Void) {
        call(path: LockboxConfig.unlockPath, completion: completion)
    }

    /// Manual 1-second test click (/pulse) - the "unlock now" button.
    func manualPulse(completion: @escaping (Bool) -> Void = { _ in }) {
        call(path: LockboxConfig.manualPulsePath, completion: completion)
    }

    private func call(path: String, attempt: Int = 1, completion: @escaping (Bool) -> Void) {
        guard let url = URL(string: "http://\(LockboxSecrets.esp32Host)\(path)") else {
            completion(false)
            return
        }
        session.dataTask(with: url) { [weak self] _, response, error in
            let ok = error == nil && (response as? HTTPURLResponse)?.statusCode == 200
            if ok {
                DispatchQueue.main.async { completion(true) }
                return
            }
            print("[lockbox] ESP32 \(path) failed (attempt \(attempt)): \(error?.localizedDescription ?? "HTTP error")")
            // Same contract as the Python client: up to 1 + esp32Retries
            // attempts per decision, so a single dropped LAN packet doesn't
            // cost the courier 5+ seconds of a closed box.
            if attempt <= LockboxConfig.esp32Retries, let self = self {
                self.call(path: path, attempt: attempt + 1, completion: completion)
            } else {
                DispatchQueue.main.async { completion(false) }
            }
        }.resume()
    }
}

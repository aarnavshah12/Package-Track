//
//  WorkflowClient.swift
//  Posts frames to the hosted Roboflow workflow (aarnavs-space/package-track)
//  with the exact JSON payload the Phase B Mac client sends, and parses the
//  per-frame facts out of the response.
//

import UIKit

final class WorkflowClient {

    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = LockboxConfig.workflowTimeout
        return cfg
    }().toSession()

    /// One workflow call. clientEvent != "none" fires the notification branch
    /// (vision event + dataset upload) exactly once, server-side.
    func infer(image: UIImage,
               clientEvent: String = "none",
               completion: @escaping (Result<FrameFacts, Error>) -> Void) {
        // Resize + JPEG + base64 + JSON are heavy; keep them off the main thread.
        DispatchQueue.global(qos: .utility).async { [self] in
            self.buildAndSend(image: image, clientEvent: clientEvent, completion: completion)
        }
    }

    private func buildAndSend(image: UIImage,
                              clientEvent: String,
                              completion: @escaping (Result<FrameFacts, Error>) -> Void) {

        let prepared = image.resizedToMaxDimension(1280)
        guard let jpeg = prepared.jpegData(compressionQuality: 0.9) else {
            completion(.failure(LockboxError.encodeFailed))
            return
        }
        if clientEvent == "none" {
            DashboardClient.shared.postSnapshot(jpeg)   // live view mirror (best-effort)
        }
        let width = Int(prepared.size.width * prepared.scale)
        let height = Int(prepared.size.height * prepared.scale)
        let isEvent = clientEvent != "none"

        let payload: [String: Any] = [
            "api_key": LockboxSecrets.roboflowAPIKey,
            "use_cache": true,
            "inputs": [
                "image": ["type": "base64", "value": jpeg.base64EncodedString()],
                "model_id": LockboxConfig.modelId,
                "zone": LockboxConfig.zonePixels(width: width, height: height),
                "min_confidence": LockboxConfig.minConfidence,
                "person_confidence": LockboxConfig.personConfidence,
                "package_confidence": LockboxConfig.packageConfidence,
                "raw_classes": LockboxConfig.rawClasses,
                "client_event": clientEvent,
                "disable_upload": !isEvent,
                "ntfy_topic": LockboxSecrets.ntfyTopic,
            ] as [String: Any],
            "excluded_fields": ["output_image", "predictions", "zone_predictions"],
        ]

        var request = URLRequest(url: LockboxConfig.workflowURL)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        session.dataTask(with: request) { data, response, error in
            if let error = error {
                completion(.failure(error))
                return
            }
            guard let http = response as? HTTPURLResponse, let data = data else {
                completion(.failure(LockboxError.badResponse("no response")))
                return
            }
            guard http.statusCode == 200 else {
                let body = String(data: data.prefix(300), encoding: .utf8) ?? ""
                completion(.failure(LockboxError.badResponse("HTTP \(http.statusCode): \(body)")))
                return
            }
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let outputs = json["outputs"] as? [[String: Any]],
                  let out = outputs.first else {
                completion(.failure(LockboxError.badResponse("unparseable body")))
                return
            }
            var facts = FrameFacts()
            facts.personInZone = out["person_in_zone"] as? Bool ?? false
            facts.packageInZone = out["package_in_zone"] as? Bool ?? false
            facts.personWithPackage = out["person_with_package"] as? Bool ?? false
            facts.personCount = out["person_count"] as? Int ?? 0
            facts.packageCount = out["package_count"] as? Int ?? 0
            facts.maxPersonConfidence = out["max_person_confidence"] as? Double ?? 0
            facts.maxPackageConfidence = out["max_package_confidence"] as? Double ?? 0
            completion(.success(facts))
        }.resume()
    }
}

enum LockboxError: Error, CustomStringConvertible {
    case encodeFailed
    case badResponse(String)

    var description: String {
        switch self {
        case .encodeFailed: return "could not JPEG-encode the frame"
        case .badResponse(let detail): return "workflow error: \(detail)"
        }
    }
}

private extension URLSessionConfiguration {
    func toSession() -> URLSession { URLSession(configuration: self) }
}

extension UIImage {
    /// Downscale so the longest side is at most maxDim (matches prepare_frame
    /// in lockbox_client.py: consistent pixel space, ~10x smaller upload).
    func resizedToMaxDimension(_ maxDim: CGFloat) -> UIImage {
        let longest = max(size.width, size.height)
        guard longest > maxDim else { return self }
        let ratio = maxDim / longest
        let newSize = CGSize(width: size.width * ratio, height: size.height * ratio)
        let renderer = UIGraphicsImageRenderer(size: newSize, format: {
            let f = UIGraphicsImageRendererFormat.default()
            f.scale = 1
            return f
        }())
        return renderer.image { _ in draw(in: CGRect(origin: .zero, size: newSize)) }
    }
}

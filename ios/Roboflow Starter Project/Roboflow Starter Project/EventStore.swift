//
//  EventStore.swift
//  Local activity feed: every notable moment (box opened, delivery confirmed,
//  delivery failed) is saved to the app's Documents folder with its photo.
//  The cloud keeps its own copy in Roboflow Vision Events; this is the
//  on-phone view of the same story.
//

import UIKit

struct LockboxEvent: Codable {
    let id: String
    let event: String
    let date: Date
    let imageFile: String

    var friendlyTitle: String {
        switch event {
        case "delivery_confirmed": return "📦 Package delivered"
        case "delivery_failed_package_on_ground": return "⚠️ Package left outside"
        case "box_opened": return "🔓 Box opened for delivery"
        default: return event
        }
    }
}

final class EventStore {
    static let shared = EventStore()

    private let dir: URL = FileManager.default
        .urls(for: .documentDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("events", isDirectory: true)
    private var indexURL: URL { dir.appendingPathComponent("index.json") }

    private(set) var events: [LockboxEvent] = []

    init() {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if let data = try? Data(contentsOf: indexURL),
           let decoded = try? JSONDecoder().decode([LockboxEvent].self, from: data) {
            events = decoded
        }
    }

    func record(event: String, image: UIImage?) {
        let id = UUID().uuidString
        let file = "\(id).jpg"
        if let jpeg = image?.eventJPEG() {
            try? jpeg.write(to: dir.appendingPathComponent(file))
        }
        events.insert(LockboxEvent(id: id, event: event, date: Date(), imageFile: file), at: 0)
        if events.count > 200 { events.removeLast(events.count - 200) }
        if let data = try? JSONEncoder().encode(events) {
            try? data.write(to: indexURL)
        }
    }

    func image(for event: LockboxEvent) -> UIImage? {
        UIImage(contentsOfFile: dir.appendingPathComponent(event.imageFile).path)
    }
}


extension UIImage {
    /// Event photos are records, not wallpapers: ~960px and modest quality
    /// keeps each one around 100-150 KB instead of megabytes.
    func eventJPEG() -> Data? {
        resizedToMaxDimension(960).jpegData(compressionQuality: 0.6)
    }
}

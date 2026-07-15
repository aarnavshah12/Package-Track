//
//  ActivityViewController.swift
//  The activity feed: photos of every delivery moment, newest first.
//

import UIKit

final class ActivityViewController: UITableViewController {

    private let formatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f
    }()

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "Activity"
        tableView.register(UITableViewCell.self, forCellReuseIdentifier: "event")
        tableView.rowHeight = 84
        navigationItem.rightBarButtonItem = UIBarButtonItem(
            barButtonSystemItem: .close, target: self, action: #selector(closeTapped))
    }

    @objc private func closeTapped() { dismiss(animated: true) }

    override func tableView(_ tableView: UITableView, numberOfRowsInSection section: Int) -> Int {
        max(EventStore.shared.events.count, 1)
    }

    override func tableView(_ tableView: UITableView, cellForRowAt indexPath: IndexPath) -> UITableViewCell {
        let cell = tableView.dequeueReusableCell(withIdentifier: "event", for: indexPath)
        var config = cell.defaultContentConfiguration()

        let events = EventStore.shared.events
        if events.isEmpty {
            config.text = "No activity yet"
            config.secondaryText = "Delivery moments will appear here with photos"
        } else {
            let event = events[indexPath.row]
            config.text = event.friendlyTitle
            config.secondaryText = formatter.string(from: event.date)
            if let image = EventStore.shared.image(for: event) {
                config.image = image
                config.imageProperties.maximumSize = CGSize(width: 96, height: 68)
                config.imageProperties.cornerRadius = 8
            }
        }
        cell.contentConfiguration = config
        cell.selectionStyle = .none
        return cell
    }
}

import AppKit
import Darwin
import Foundation
import UserNotifications

// A one-request app, launched as a bundle. Content crosses only an owner-local
// socket, never command-line arguments, files, logs or a remote service.
final class Notifier: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private var descriptor: Int32 = -1
    private var finished = false
    private let center = UNUserNotificationCenter.current()
    private var operation = "status"
    private var notificationID: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        center.delegate = self
        DispatchQueue.main.asyncAfter(deadline: .now() + 55) { self.finish("unconfirmed") }
        do {
            let args = CommandLine.arguments
            guard args.count == 3, args[1] == "--socket" else { throw Failure.input }
            try connect(args[2])
            let request = try readRequest()
            guard let action = request["action"] as? String,
                  ["status", "authorize", "post"].contains(action) else { throw Failure.input }
            operation = action
            if action != "post" {
                guard Set(request.keys) == ["action"] else { throw Failure.input }
            } else {
                guard Set(request.keys) == ["action", "id", "body"],
                      let id = request["id"] as? String,
                      id.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil,
                      let body = request["body"] as? String, !body.isEmpty,
                      body.utf8.count <= 4096,
                      !body.unicodeScalars.contains(where: { $0.value < 32 && $0.value != 10 })
                else { throw Failure.input }
                notificationID = id
            }
            center.getNotificationSettings { settings in
                DispatchQueue.main.async { self.handle(request, settings) }
            }
        } catch { finish("invalid_request") }
    }

    private func handle(_ request: [String: Any], _ settings: UNNotificationSettings) {
        if operation == "status" {
            finish("status", authorization: authorization(settings.authorizationStatus))
        } else if operation == "authorize" {
            // Only the explicit CLI authorization command reaches this branch.
            NSApp.activate(ignoringOtherApps: true)
            center.requestAuthorization(options: [.alert]) { _, error in
                guard error == nil else { self.finish("authorization_failed"); return }
                self.center.getNotificationSettings { updated in
                    self.finish("status", authorization: self.authorization(updated.authorizationStatus))
                }
            }
        } else {
            guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional,
                  settings.notificationCenterSetting == .enabled else {
                finish("permission_denied", authorization: authorization(settings.authorizationStatus))
                return
            }
            let content = UNMutableNotificationContent()
            content.title = "大黄蜂提醒"
            content.body = request["body"] as! String
            // Silent desktop delivery. Never open capture, speak or invoke tools.
            content.sound = nil
            let id = notificationID!
            center.getDeliveredNotifications { notifications in
                if notifications.contains(where: { $0.request.identifier == id }) {
                    self.finish("notification_center")
                    return
                }
                let item = UNNotificationRequest(identifier: id, content: content, trigger: nil)
                self.center.add(item) { error in
                    if error != nil { self.finish("rejected"); return }
                    self.checkDelivery(id, deadline: Date().addingTimeInterval(3))
                }
            }
        }
    }

    private func checkDelivery(_ id: String, deadline: Date) {
        center.getDeliveredNotifications { items in
            if items.contains(where: { $0.request.identifier == id }) {
                self.finish("notification_center")
            } else if Date() >= deadline {
                // add() success alone is not proof the notification was displayed.
                self.finish("submitted_unconfirmed")
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                    self.checkDelivery(id, deadline: deadline)
                }
            }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.list, .banner])
    }

    private func authorization(_ value: UNAuthorizationStatus) -> String {
        switch value {
        case .notDetermined: return "not_determined"
        case .denied: return "denied"
        case .authorized: return "authorized"
        case .provisional: return "provisional"
        @unknown default: return "unknown"
        }
    }

    private func connect(_ path: String) throws {
        var address = sockaddr_un()
        let bytes = Array(path.utf8) + [UInt8(0)]
        guard path.hasPrefix("/"), bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { throw Failure.input }
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes) }
        descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw Failure.socket }
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        var uid: uid_t = 0, gid: gid_t = 0
        guard result == 0, getpeereid(descriptor, &uid, &gid) == 0, uid == geteuid() else { throw Failure.socket }
        var noSignal: Int32 = 1
        setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        var timeout = timeval(tv_sec: 5, tv_usec: 0)
        setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
    }

    private func readRequest() throws -> [String: Any] {
        var data = Data()
        var byte: UInt8 = 0
        while data.count <= 8192 {
            guard Darwin.read(descriptor, &byte, 1) == 1 else { throw Failure.input }
            if byte == 10 {
                guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw Failure.input }
                return value
            }
            data.append(byte)
        }
        throw Failure.input
    }

    private func finish(_ result: String, authorization: String? = nil) {
        DispatchQueue.main.async {
            guard !self.finished else { return }
            self.finished = true
            var value: [String: Any] = ["protocol": 1, "result": result]
            if let authorization = authorization { value["authorization"] = authorization }
            if let id = self.notificationID { value["id"] = id }
            if self.descriptor >= 0 {
                if let data = try? JSONSerialization.data(withJSONObject: value) {
                    var raw = Array(data) + [UInt8(10)]
                    while !raw.isEmpty {
                        let sent = raw.withUnsafeBytes { Darwin.write(self.descriptor, $0.baseAddress!, $0.count) }
                        if sent <= 0 { break }
                        raw.removeFirst(sent)
                    }
                }
                Darwin.close(self.descriptor)
                self.descriptor = -1
            }
            NSApp.terminate(nil)
        }
    }
    private enum Failure: Error { case input, socket }
}

let application = NSApplication.shared
let delegate = Notifier()
application.delegate = delegate
application.run()

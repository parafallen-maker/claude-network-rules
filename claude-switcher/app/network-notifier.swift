import AppKit
import Foundation
import UserNotifications
import Darwin

// Run only in the account owner's GUI session. The root monitor never launches
// a user-writable executable and this process has no authority to stop Claude.
private struct MonitorEvent: Decodable {
    let id: String
    let time: Double
    let kind: String
    let claude_stopped: Bool
    let stop_failed: Bool?

    static let kinds: Set<String> = ["vpn_disconnected", "node_changed", "exit_ip_changed", "monitor_unavailable"]

    static func decode(_ data: Data, now: Double = Date().timeIntervalSince1970) throws -> MonitorEvent? {
        struct Envelope: Decodable { let schema: Int; let latest: MonitorEvent? }
        let envelope = try JSONDecoder().decode(Envelope.self, from: data)
        guard envelope.schema == 1 else { throw NotifierError.invalidEvent }
        guard let event = envelope.latest else { return nil }
        // A random event identifier must never contain a node name or address.
        guard event.id.range(of: "^[A-Za-z0-9_-]{8,128}$", options: .regularExpression) != nil,
              kinds.contains(event.kind), event.time.isFinite,
              event.time > 0, event.time <= now + 300 else { throw NotifierError.invalidEvent }
        return event
    }

    var requiresNotification: Bool { claude_stopped || stop_failed == true }
    var title: String { "Claude 网络防护" }
    var body: String {
        let reason: String
        switch kind {
        case "vpn_disconnected": reason = "VPN 或代理连接已断开。"
        case "node_changed": reason = "代理节点发生变化。"
        case "exit_ip_changed": reason = "代理出口 IP 发生变化。"
        default: reason = "无法确认网络防护状态。"
        }
        if stop_failed == true { return reason + "未能确认 Claude 已停止，请立即检查。" }
        return reason + (claude_stopped ? "已停止 Claude，请检查网络后重新启动。" : "当前没有需要停止的 Claude 进程，请检查网络后再启动。")
    }
}

private enum NotifierError: Error { case invalidEvent, unsafePath, stateUnavailable }

private struct NotifierStatus: Codable {
    var schema = 1
    var authorization = "unknown"
    var alert_setting = "unknown"
    var sound_setting = "unknown"
    var notification_center_setting = "unknown"
    var last_event_id: String?
    var last_event_kind: String?
    var last_event_time: Double?
    var last_stop_failed: Bool?
    var last_delivery: Double?
    var delivery_state = "none"
    var delivery_error: String?
    var handled_event_ids: [String] = []
    var last_heartbeat: Double?
    var event_read_error: String?
    var last_test_id: String?
    var last_test_delivery: Double?
    var last_test_state: String?
    var last_test_error: String?

    mutating func consume(_ event: MonitorEvent) -> Bool {
        guard !handled_event_ids.contains(event.id) else { return false }
        handled_event_ids = Array((handled_event_ids + [event.id]).suffix(128))
        last_event_id = event.id
        last_event_kind = event.kind
        last_event_time = event.time
        last_stop_failed = event.stop_failed ?? false
        last_delivery = nil
        // Consume legacy idle events too, so an upgrade/restart cannot replay
        // a warning that did not affect Claude. Manual tests bypass this path.
        delivery_state = event.requiresNotification ? "pending" : "suppressed"
        delivery_error = nil
        return event.requiresNotification
    }
}

private final class StatusStore {
    private let base = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude-switcher", isDirectory: true)
    private var path: URL { base.appendingPathComponent("network-notifier-status.json") }

    func read() -> NotifierStatus {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: path.path),
              attributes[.type] as? FileAttributeType == .typeRegular,
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              let data = try? Data(contentsOf: path), data.count <= 65536,
              let result = try? JSONDecoder().decode(NotifierStatus.self, from: data), result.schema == 1 else {
            return NotifierStatus()
        }
        return result
    }

    @discardableResult
    func update(_ change: (inout NotifierStatus) -> Void) throws -> NotifierStatus {
        let fm = FileManager.default
        try fm.createDirectory(at: base, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        var info = stat()
        guard lstat(base.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR,
              info.st_uid == getuid(), info.st_mode & 0o022 == 0 else { throw NotifierError.unsafePath }
        let lock = open(base.appendingPathComponent(".network-notifier-status.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
        guard lock >= 0 else { throw NotifierError.stateUnavailable }
        defer { flock(lock, LOCK_UN); close(lock) }
        guard flock(lock, LOCK_EX) == 0 else { throw NotifierError.stateUnavailable }
        var status = read()
        change(&status)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let bytes = try encoder.encode(status)
        let temp = base.appendingPathComponent(".network-notifier-" + UUID().uuidString)
        let fd = open(temp.path, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw NotifierError.stateUnavailable }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close(); try? fm.removeItem(at: temp) }
        try handle.write(contentsOf: bytes)
        try handle.synchronize()
        guard rename(temp.path, path.path) == 0 else { throw NotifierError.stateUnavailable }
        return status
    }
}

private func errorCode(_ error: Error) -> String {
    // Do not put arbitrary localized errors, paths, IPs or event text in status.
    let error = error as NSError
    let domain = error.domain.filter { $0.isLetter || $0.isNumber || $0 == "_" }
    return String(domain.prefix(64)) + ":" + String(error.code)
}

private func authorizationName(_ settings: UNNotificationSettings) -> String {
    switch settings.authorizationStatus {
    case .notDetermined: return "not_determined"
    case .denied: return "denied"
    case .authorized: return "authorized"
    case .provisional: return "provisional"
    @unknown default: return "unknown"
    }
}

private func settingName(_ setting: UNNotificationSetting) -> String {
    switch setting {
    case .enabled: return "enabled"
    case .disabled: return "disabled"
    case .notSupported: return "not_supported"
    @unknown default: return "unknown"
    }
}

private final class NetworkNotifier: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    private let store = StatusStore()
    private let mode: String
    private var timer: Timer?
    private var inFlight = false
    private var ticks = 0

    init(mode: String) { self.mode = mode; super.init() }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        center.delegate = self
        if mode == "--status" {
            center.getNotificationSettings { [weak self] settings in
                guard let self = self else { return }
                var status = self.store.read()
                Self.apply(settings, to: &status)
                self.printStatus(status)
                DispatchQueue.main.async { NSApplication.shared.terminate(nil) }
            }
            return
        }
        center.getNotificationSettings { [weak self] settings in
            guard let self = self else { return }
            if settings.authorizationStatus == .notDetermined {
                self.center.requestAuthorization(options: [.alert, .sound]) { _, error in
                    if let error = error { self.save { $0.delivery_error = "authorization_request:" + errorCode(error) } }
                    self.begin()
                }
            } else {
                self.begin()
            }
        }
    }

    private static func apply(_ settings: UNNotificationSettings, to status: inout NotifierStatus) {
        status.authorization = authorizationName(settings)
        status.alert_setting = settingName(settings.alertSetting)
        status.sound_setting = settingName(settings.soundSetting)
        status.notification_center_setting = settingName(settings.notificationCenterSetting)
    }

    private func save(_ mutation: (inout NotifierStatus) -> Void) {
        do { try store.update(mutation) }
        catch { fputs("NetworkNotifier: status_write_failed\n", stderr) }
    }

    private func begin() {
        DispatchQueue.main.async {
            if self.mode == "--test-notification" {
                self.sendTest()
                return
            }
            self.refreshSettings()
            self.poll()
            self.timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.poll() }
        }
    }

    private func refreshSettings() {
        center.getNotificationSettings { [weak self] settings in
            self?.save { Self.apply(settings, to: &$0); $0.last_heartbeat = Date().timeIntervalSince1970 }
        }
    }

    private func readEvent() throws -> MonitorEvent? {
        let directory = "/var/run/claude-switcher-monitor"
        var info = stat()
        if lstat(directory, &info) != 0 {
            if errno == ENOENT { return nil }
            throw NotifierError.unsafePath
        }
        guard (info.st_mode & S_IFMT) == S_IFDIR, info.st_uid == 0, info.st_mode & 0o022 == 0 else {
            throw NotifierError.unsafePath
        }
        let fd = open(directory + "/events-\(getuid()).json", O_RDONLY | O_NOFOLLOW)
        if fd < 0 {
            if errno == ENOENT { return nil }
            throw NotifierError.unsafePath
        }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == 0, info.st_mode & 0o022 == 0,
              info.st_size > 0, info.st_size <= 16384 else { throw NotifierError.unsafePath }
        let data = try handle.read(upToCount: 16385) ?? Data()
        guard data.count <= 16384 else { throw NotifierError.invalidEvent }
        return try MonitorEvent.decode(data)
    }

    private func poll() {
        ticks += 1
        if ticks % 15 == 0 { refreshSettings() }
        guard !inFlight else { return }
        do {
            guard let event = try readEvent() else { return }
            guard !store.read().handled_event_ids.contains(event.id) else { return }
            var fresh = false
            // Persist before scheduling: repeated polls/restarts do not display
            // the same event again. A crash in between remains unconfirmed.
            try store.update {
                $0.event_read_error = nil
                fresh = $0.consume(event)
                if fresh && Date().timeIntervalSince1970 - event.time > 300 {
                    $0.delivery_state = "stale"
                    $0.delivery_error = "event_older_than_5_minutes"
                    fresh = false
                }
            }
            guard fresh else { return }
            inFlight = true
            deliver(id: event.id, title: event.title, body: event.body, test: false)
        } catch {
            save { $0.event_read_error = "event_file_unavailable_or_invalid" }
        }
    }

    private func sendTest() {
        let id = "test-" + UUID().uuidString
        save { $0.last_test_id = id; $0.last_test_state = "pending"; $0.last_test_error = nil; $0.last_test_delivery = nil }
        deliver(id: id, title: "Claude 网络防护 · 测试通知", body: "这是一条系统通知测试，不代表 VPN 发生故障，也没有停止 Claude。", test: true)
    }

    private func record(id: String, state: String, error: String?, observed: Bool = false, test: Bool) {
        save {
            if test {
                guard $0.last_test_id == id else { return }
                $0.last_test_state = state; $0.last_test_error = error
                if observed { $0.last_test_delivery = Date().timeIntervalSince1970 }
            } else {
                guard $0.last_event_id == id else { return }
                $0.delivery_state = state; $0.delivery_error = error
                if observed { $0.last_delivery = Date().timeIntervalSince1970 }
            }
        }
    }

    private func finish(test: Bool) {
        DispatchQueue.main.async {
            self.inFlight = false
            if test {
                let status = self.store.read()
                self.printStatus(status)
                // A failed, denied or unconfirmed test must not exit as passed.
                // Even success proves only Notification Center observation;
                // Focus settings may still suppress a visible banner.
                let observed = status.last_test_state == "observed_in_notification_center"
                    && status.authorization == "authorized" && status.alert_setting == "enabled"
                exit(observed ? 0 : 1)
            }
        }
    }

    private func deliver(id: String, title: String, body: String, test: Bool) {
        center.getNotificationSettings { [weak self] settings in
            guard let self = self else { return }
            self.save { Self.apply(settings, to: &$0) }
            guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                self.record(id: id, state: "denied", error: "notification_permission_not_granted", test: test)
                self.finish(test: test)
                return
            }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            content.sound = .default
            content.threadIdentifier = "claude-network-monitor"
            let request = UNNotificationRequest(identifier: "claude-monitor-" + id, content: content, trigger: nil)
            self.center.add(request) { error in
                if let error = error {
                    self.record(id: id, state: "failed", error: errorCode(error), test: test)
                    self.finish(test: test)
                    return
                }
                let visibilityError = settings.alertSetting == .enabled && settings.authorizationStatus == .authorized ? nil : "banner_alerts_not_enabled"
                self.record(id: id, state: "accepted", error: visibilityError, test: test)
                // Acceptance means scheduled, not that the person saw a banner.
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                    self.center.getDeliveredNotifications { delivered in
                        let seen = delivered.contains { $0.request.identifier == request.identifier }
                        self.record(id: id, state: seen ? "observed_in_notification_center" : "accepted_unconfirmed",
                                    error: seen ? visibilityError : (visibilityError ?? "delivery_not_observed"), observed: seen, test: test)
                        self.finish(test: test)
                    }
                }
            }
        }
    }

    private func printStatus(_ status: NotifierStatus) {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        if let data = try? encoder.encode(status), let text = String(data: data, encoding: .utf8) { print(text) }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }
}

private func selfTest() throws {
    let now = Date().timeIntervalSince1970
    let body: [String: Any] = ["schema": 1, "latest": ["id": "event-12345678", "time": now, "kind": "node_changed", "message": "UNTRUSTED MESSAGE", "claude_stopped": true]]
    let event = try MonitorEvent.decode(JSONSerialization.data(withJSONObject: body), now: now)!
    precondition(!event.body.contains("UNTRUSTED"))
    var state = NotifierStatus()
    precondition(state.consume(event))
    precondition(!state.consume(event))
    let restored = try JSONDecoder().decode(NotifierStatus.self, from: JSONEncoder().encode(state))
    var afterRestart = restored
    precondition(!afterRestart.consume(event))
    for bad in ["unexpected", "vpn_disconnected\n"] {
        let invalid: [String: Any] = ["schema": 1, "latest": ["id": "event-12345678", "time": now, "kind": bad, "claude_stopped": true]]
        do { _ = try MonitorEvent.decode(JSONSerialization.data(withJSONObject: invalid), now: now); preconditionFailure("Invalid kind accepted") }
        catch NotifierError.invalidEvent { }
    }
    precondition(state.delivery_state == "pending" && state.last_delivery == nil)
    let failure: [String: Any] = ["schema": 1, "latest": ["id": "event-stopfailure", "time": now, "kind": "node_changed", "claude_stopped": false, "stop_failed": true]]
    let failedStop = try MonitorEvent.decode(JSONSerialization.data(withJSONObject: failure), now: now)!
    precondition(failedStop.body.contains("未能确认 Claude 已停止") && !failedStop.body.contains("当前没有"))
    precondition(state.consume(failedStop) && state.last_stop_failed == true)
    for (index, kind) in MonitorEvent.kinds.sorted().enumerated() {
        // Older monitor versions omitted stop_failed; both representations of
        // an idle event must be persisted as handled without scheduling a banner.
        for includeFailure in [false, true] {
            let id = "idle-event-\(index)-\(includeFailure)"
            var latest: [String: Any] = ["id": id, "time": now, "kind": kind, "claude_stopped": false]
            if includeFailure { latest["stop_failed"] = false }
            let idle = try MonitorEvent.decode(JSONSerialization.data(withJSONObject: ["schema": 1, "latest": latest]), now: now)!
            precondition(!idle.requiresNotification && !state.consume(idle))
            precondition(state.handled_event_ids.contains(id) && state.last_event_id == id)
            precondition(state.delivery_state == "suppressed" && state.last_delivery == nil && state.delivery_error == nil)
            var restarted = try JSONDecoder().decode(NotifierStatus.self, from: JSONEncoder().encode(state))
            precondition(!restarted.consume(idle) && restarted.delivery_state == "suppressed")
        }
    }
    var actionableState = NotifierStatus()
    precondition(actionableState.consume(event) && actionableState.delivery_state == "pending")
    precondition(actionableState.consume(failedStop) && actionableState.delivery_state == "pending")
    print("OK: notifier event parsing, fixed text, persistent deduplication, idle suppression and actionable-event checks")
}

let arguments = Array(CommandLine.arguments.dropFirst())
let mode = arguments.first ?? "--watch"
guard arguments.count <= 1, ["--watch", "--status", "--test-notification", "--self-test"].contains(mode) else {
    fputs("Usage: NetworkNotifier [--watch|--status|--test-notification|--self-test]\n", stderr)
    exit(2)
}
guard getuid() != 0 else { fputs("NetworkNotifier must run in the user's GUI session.\n", stderr); exit(1) }
if mode == "--self-test" {
    do { try selfTest(); exit(0) }
    catch { fputs("NetworkNotifier self-test failed.\n", stderr); exit(1) }
}
guard Bundle.main.bundleIdentifier == "local.claude-switcher.network-notifier" else {
    fputs("Run the signed NetworkNotifier.app bundle, not an unbundled binary.\n", stderr)
    exit(1)
}
let application = NSApplication.shared
private let delegate = NetworkNotifier(mode: mode)
application.delegate = delegate
application.run()

import SwiftUI
import AppKit
import UniformTypeIdentifiers

// MARK: - 模型

enum RowState { case pending, ok, warn, fail, skip }

struct CheckRow: Identifiable {
    let id = UUID()
    let name: String
    let icon: String
    var state: RowState = .pending
    var detail: String = ""
}

enum Phase { case checking, ready, failed, switching, done, fixing }

struct CoreResult {
    let output: String
    let succeeded: Bool
}

struct BrowserProfile: Identifiable {
    let id: String
    let name: String
}

enum ConfigurationTone { case neutral, success, warning, failure }

struct AccountStamp: Equatable {
    let size: String
    let date: String
}

// 超时回调与 waitUntilExit 在不同队列上执行。
private final class ProcessDeadline {
    private let lock = NSLock()
    private var expired = false

    func expire(_ process: Process) {
        lock.lock()
        defer { lock.unlock() }
        guard process.isRunning else { return }
        expired = true
        process.terminate()
    }

    var timedOut: Bool {
        lock.lock()
        defer { lock.unlock() }
        return expired
    }
}

final class VM: ObservableObject {
    @Published var rows: [CheckRow] = [
        CheckRow(name: "代理内核", icon: "cpu"),
        CheckRow(name: "TUN 网卡", icon: "cable.connector"),
        CheckRow(name: "出口 IP", icon: "globe.americas"),
        CheckRow(name: "IP 与上次一致", icon: "checkmark.seal"),
        CheckRow(name: "时区", icon: "clock"),
        CheckRow(name: "地区", icon: "flag"),
        CheckRow(name: "IPv6 路径", icon: "6.circle"),
        CheckRow(name: "DNS 解析", icon: "magnifyingglass"),
        CheckRow(name: "WebRTC", icon: "eye.slash"),
        CheckRow(name: "保镖在线", icon: "hand.raised"),
        CheckRow(name: "规则置顶", icon: "list.number"),
        CheckRow(name: "客户端语言", icon: "abc"),
        CheckRow(name: "ASN 属性", icon: "building.columns"),
        CheckRow(name: "生态可达性", icon: "antenna.radiowaves.left.and.right"),
    ]
    @Published var phase: Phase = .checking
    @Published var current = "?"
    @Published var archived: Set<String> = []
    @Published var stamps: [String: AccountStamp] = [:]
    @Published var banner = ""
    @Published var note = ""
    @Published var fingerprint = ""
    @Published private(set) var isBusy = false
    @Published private(set) var lastCheckSummary = "尚未检查"
    @Published var updateText = ""
    @Published var showUpdateSheet = false
    @Published private(set) var coreMissing = false
    @Published private(set) var currentClient = ""
    @Published private(set) var isSwitchingClient = false
    @Published var disabledChecks: Set<String> = []
    @Published var allowOptionalFailures = false
    @Published var loggingEnabled = true
    @Published var settingsMessage = ""
    @Published private(set) var settingsBusy = false
    @Published var showSettingsSheet = false
    @Published var blockSTUN = false
    @Published var stunPolicyMessage = ""
    @Published private(set) var stunPolicyLoaded = false

    @Published var showEnvironmentSheet = false
    @Published var environmentGroup = "🤖 Claude"
    @Published var browserPort = "7898"
    @Published var configureDNS = true
    @Published var automaticGroup = true
    @Published var enableBrowserProxy = true
    @Published private(set) var environmentBusy = false
    @Published private(set) var environmentLoaded = false
    @Published private(set) var environmentMessage = ""
    @Published private(set) var environmentTone: ConfigurationTone = .neutral
    @Published private(set) var environmentChanges: [String] = []
    @Published private(set) var regionSummary = "尚未读取地区目标"
    @Published private(set) var browserBusy = false
    @Published private(set) var browserMessage = ""
    @Published private(set) var browserTone: ConfigurationTone = .neutral
    @Published private(set) var browserProfiles: [BrowserProfile] = []
    @Published private(set) var browserHasMore = false
    @Published private(set) var browserPage = 1
    private var environmentPlanSignature = ""

    static let optionalChecks: [(key: String, label: String)] = [
        ("IP一致", "IP 与上次一致"),
        ("DNS", "DNS 解析"),
        ("WebRTC", "WebRTC"),
        ("客户端语言", "客户端语言"),
        ("ASN属性", "ASN 属性"),
        ("生态可达性", "生态可达性"),
    ]

    static let clientChoices: [(key: String, label: String)] = [
        ("clash-verge", "Clash Verge"),
        ("flclash", "FlClash"),
        ("shadowrocket", "Shadowrocket"),
        ("sing-box", "sing-box"),
        ("clashx", "ClashX"),
        ("surge", "Surge"),
    ]

    let core: String
    private let checkTimeout: TimeInterval
    private let operationTimeout: TimeInterval
    private var started = false
    private var switchRequested = false
    private var checkingUpdate = false
    private var hideAfterSwitch: DispatchWorkItem?
    var onSwitchCompleted: (() -> Void)?

    init(core: String = NSHomeDirectory() + "/.local/bin/claude-switch.sh",
         checkTimeout: TimeInterval = 45, operationTimeout: TimeInterval = 180) {
        self.core = core
        self.checkTimeout = checkTimeout
        self.operationTimeout = operationTimeout
    }

    var canQuit: Bool { !switchRequested && !environmentBusy && !browserBusy && phase != .fixing && phase != .switching }

    func start() {
        guard !started else { return }
        started = true
        refresh()
    }

    func prepareToShow() {
        hideAfterSwitch?.cancel()
        if phase == .done { phase = .ready }
    }

    private func run(_ args: [String], timeout: TimeInterval = 45, environment: [String: String] = [:], input: Data? = nil, structuredOutput: Bool = false, done: @escaping (CoreResult) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("switcher-out-\(UUID().uuidString).txt")
            guard FileManager.default.createFile(atPath: tmp.path, contents: nil, attributes: [.posixPermissions: 0o600]),
                  let fh = try? FileHandle(forWritingTo: tmp) else {
                DispatchQueue.main.async { done(CoreResult(output: "无法创建检查输出文件", succeeded: false)) }
                return
            }
            defer {
                try? fh.close()
                try? FileManager.default.removeItem(at: tmp)
            }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/bash")
            p.arguments = [self.core] + args
            p.environment = ProcessInfo.processInfo.environment.merging(environment) { _, new in new }
            p.standardOutput = fh
            p.standardError = fh
            let inputPipe = input == nil ? nil : Pipe()
            if let inputPipe = inputPipe { p.standardInput = inputPipe }
            do { try p.run() } catch {
                DispatchQueue.main.async { done(CoreResult(output: "无法启动脚本：\(error.localizedDescription)", succeeded: false)) }
                return
            }
            let deadline = ProcessDeadline()
            let watchdog = DispatchWorkItem { deadline.expire(p) }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: watchdog)
            if let input = input, let inputPipe = inputPipe {
                try? inputPipe.fileHandleForWriting.write(contentsOf: input)
                try? inputPipe.fileHandleForWriting.close()
            }
            p.waitUntilExit()
            watchdog.cancel()
            let data = (try? Data(contentsOf: tmp)) ?? Data()
            let out = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            let result: CoreResult
            if deadline.timedOut {
                result = CoreResult(output: "操作超时；切换事务会在下次操作时恢复，请重新检查", succeeded: false)
            } else if p.terminationStatus != 0 {
                result = CoreResult(output: structuredOutput ? out : "脚本执行失败（退出码 \(p.terminationStatus)）\n\(out)", succeeded: false)
            } else {
                result = CoreResult(output: out, succeeded: true)
            }
            DispatchQueue.main.async { done(result) }
        }
    }

    func refresh() {
        guard !isBusy else { return }
        hideAfterSwitch?.cancel()
        if !coreAvailable() { markCoreMissing(); return }
        isBusy = true
        checkEnvironment { _ in self.isBusy = false }
    }

    private func coreAvailable() -> Bool {
        FileManager.default.isReadableFile(atPath: core)
    }

    private func markCoreMissing() {
        coreMissing = true
        phase = .failed
        lastCheckSummary = "未安装核心脚本"
        banner = "需要安装本机组件。点击“安装本机组件”，在终端完成管理员验证后返回重新检查。"
        for i in rows.indices {
            rows[i].state = .fail
            rows[i].detail = "未安装核心脚本"
        }
    }

    func refreshAccountStatus() {
        // 菜单展开时只读取本地账号状态，不触发网络审计；启动审计优先。
        guard started, !isBusy else { return }
        isBusy = true
        run(["status"], timeout: checkTimeout) { result in
            self.updateStatus(result)
            self.isBusy = false
        }
    }

    private func checkEnvironment(done: @escaping (Bool) -> Void) {
        if !coreAvailable() {
            markCoreMissing()
            done(false)
            return
        }
        coreMissing = false
        phase = .checking
        lastCheckSummary = "检查中"
        banner = ""
        note = ""
        for i in rows.indices { rows[i].state = .pending; rows[i].detail = "" }
        run(["checkdetail"], timeout: checkTimeout) { result in
            var allOk = result.succeeded
            var matched = Set<Int>()
            var duplicated = false
            var malformed = false
            let keyMap: [String: String] = [
                "代理内核": "代理内核", "TUN网卡": "TUN 网卡", "出口IP": "出口 IP",
                "IP一致": "IP 与上次一致", "时区": "时区", "地区": "地区",
                "IPv6": "IPv6 路径", "DNS": "DNS 解析", "WebRTC": "WebRTC",
                "保镖": "保镖在线", "规则置顶": "规则置顶",
                "客户端语言": "客户端语言", "ASN属性": "ASN 属性", "生态可达性": "生态可达性",
            ]
            for line in result.output.split(separator: "\n") {
                if line.hasPrefix("#") { continue }
                let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
                guard parts.count == 2, let rowName = keyMap[parts[0]],
                      let idx = self.rows.firstIndex(where: { $0.name == rowName }) else {
                    allOk = false; malformed = true; continue
                }
                // 重复行不能补齐缺失项，也不能用后来的 ok 覆盖先前的 fail。
                guard matched.insert(idx).inserted else { allOk = false; duplicated = true; continue }
                let status = parts[1].split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
                switch status.first ?? "" {
                case "ok": self.rows[idx].state = .ok
                case "warn": self.rows[idx].state = .warn
                case "skip": self.rows[idx].state = .skip
                default: self.rows[idx].state = .fail; allOk = false
                }
                let optional = Self.optionalChecks.contains { $0.key == parts[0] }
                if !optional && self.rows[idx].state != .ok {
                    self.rows[idx].state = .fail
                    allOk = false
                }
                self.rows[idx].detail = status.count > 1 ? status[1] : ""
            }
            if !result.succeeded {
                self.banner = result.output.isEmpty ? "核心脚本执行失败" : result.output
                for i in self.rows.indices where self.rows[i].state == .pending {
                    self.rows[i].state = .fail
                    self.rows[i].detail = "核心脚本执行失败"
                }
            } else if duplicated {
                self.banner = "检测输出存在重复项，请重新检查"
            } else if malformed {
                self.banner = "检测输出包含未知或无效内容，请重新检查"
            } else if matched.count != self.rows.count {
                self.banner = "检测输出不完整(\(matched.count)/\(self.rows.count))，请点重新检查"
            }
            let passed = allOk && matched.count == self.rows.count
            self.lastCheckSummary = passed
                ? (self.rows.contains { $0.state == .warn }
                    ? "有警告"
                    : (self.rows.contains { $0.state == .skip } ? "通过（含跳过）" : "通过"))
                : "失败"
            self.run(["get-client"], timeout: self.checkTimeout) { clientResult in
                self.updateClient(clientResult)
                self.run(["status"], timeout: self.checkTimeout) { status in
                    let statusOK = self.updateStatus(status)
                    if !statusOK {
                        if !passed || self.banner.isEmpty {
                            self.banner = "无法读取账号状态，请重新检查"
                        }
                        self.lastCheckSummary = "失败"
                    }
                    self.phase = passed && statusOK ? .ready : .failed
                    done(passed && statusOK)
                }
            }
        }
    }

    @discardableResult
    private func updateStatus(_ result: CoreResult) -> Bool {
        let fields = result.output.split(whereSeparator: { $0.isWhitespace }).reduce(into: [String: String]()) { values, field in
            let pair = field.split(separator: "=", maxSplits: 1).map(String.init)
            if pair.count == 2 { values[pair[0]] = pair[1] }
        }
        func reset() -> Bool {
            current = "未知"; archived = []; stamps = [:]
            return false
        }
        guard result.succeeded, let account = fields["current"],
              ["A", "B", "C", "D", "none"].contains(account) else {
            return reset()
        }
        if let pending = fields["pending"], !["0", "1"].contains(pending) { return reset() }
        let recoveryNote = "存在中断的账号事务；下次切换会先恢复。也可运行 recover 单独恢复。"
        if fields["pending"] == "1" { note = recoveryNote }
        else if note == recoveryNote { note = "" }
        var known = Set<String>()
        if let list = fields["accounts"] {
            if list != "none" {
                for piece in list.split(separator: ",", omittingEmptySubsequences: true) {
                    let letter = String(piece)
                    guard Self.accountLetters.contains(letter) else { return reset() }
                    known.insert(letter)
                }
            }
        } else {
            // 旧版核心只输出 current/hasA/hasB,继续兼容已安装的旧脚本。
            for letter in ["A", "B"] {
                guard let flag = fields["has\(letter)"], ["0", "1"].contains(flag) else { return reset() }
                if flag == "1" { known.insert(letter) }
            }
        }
        var parsed: [String: AccountStamp] = [:]
        for letter in Self.accountLetters {
            guard let raw = fields["acc\(letter)"] else { continue }
            let parts = raw.split(separator: "@", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return reset() }
            parsed[letter] = AccountStamp(size: String(parts[0]), date: String(parts[1]))
        }
        current = account == "none" ? "未初始化" : account
        stamps = parsed
        archived = known
        if let blocked = fields["blocked"] {
            guard ["install", "update"].contains(blocked) else { return reset() }
            phase = .failed
            lastCheckSummary = "组件待恢复"
            banner = blocked == "install"
                ? "安装事务尚未完成，请先使用原安装包运行 recover-install"
                : "更新事务尚未完成，请先运行 component-manager.py recover"
            return false
        }
        return true
    }

    static let accountLetters = ["A", "B", "C", "D"]

    private func updateClient(_ result: CoreResult) {
        guard result.succeeded else { return }
        for line in result.output.split(separator: "\n") {
            let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
            if parts.count == 2 && parts[0] == "client" {
                currentClient = parts[1]
            }
        }
    }

    @discardableResult
    private func readSTUNPolicy() -> String? {
        let path = NSHomeDirectory() + "/.claude-switcher/webrtc-policy"
        let policy: String
        if FileManager.default.fileExists(atPath: path) {
            guard let raw = try? String(contentsOfFile: path, encoding: .utf8) else {
                stunPolicyLoaded = false
                stunPolicyMessage = "无法读取 STUN 策略，请检查配置文件权限"
                return nil
            }
            policy = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        } else {
            policy = "proxy"
        }
        guard policy == "proxy" || policy == "reject" else {
            stunPolicyLoaded = false
            stunPolicyMessage = "STUN 策略配置无效，未应用更改"
            return nil
        }
        blockSTUN = policy == "reject"
        stunPolicyLoaded = true
        return policy
    }

    func applySTUNPolicy() {
        guard !isBusy, !settingsBusy, stunPolicyLoaded, coreAvailable() else { return }
        guard currentClient == "clash-verge" || currentClient == "flclash" else {
            stunPolicyMessage = "当前代理客户端不支持自动切换 STUN 策略"
            return
        }
        let requested = blockSTUN ? "reject" : "proxy"
        hideAfterSwitch?.cancel()
        isBusy = true
        settingsBusy = true
        phase = .fixing
        banner = "正在应用 STUN 策略并验证代理规则…"
        stunPolicyMessage = "正在应用；可能重启代理，随后重新执行全部检查…"
        run(["fix"], timeout: operationTimeout, environment: ["WEBRTC_POLICY": requested]) { result in
            let saved = self.readSTUNPolicy()
            let applied = result.succeeded && saved == requested
            let failure = result.succeeded
                ? "策略未生效。Clash Verge 需要处于规则模式；请检查当前模式与规则加载状态。"
                : result.output
            self.checkEnvironment { passed in
                self.isBusy = false
                self.settingsBusy = false
                if applied {
                    self.stunPolicyMessage = passed
                        ? "STUN 策略已应用，全部环境检查已完成"
                        : "STUN 策略已写入，但环境检查未通过；请查看主界面检查结果"
                } else {
                    self.stunPolicyMessage = "应用未完成：" + failure
                }
            }
        }
    }

    func loadCheckConfig() {
        guard !isBusy, !settingsBusy, coreAvailable() else { return }
        settingsBusy = true
        settingsMessage = "正在读取设置…"
        stunPolicyMessage = ""
        readSTUNPolicy()
        run(["get-check-config"]) { result in
            self.settingsBusy = false
            guard result.succeeded,
                  let data = result.output.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data),
                  let config = object as? [String: Any],
                  let disabled = config["disabled"] as? [String],
                  let allow = config["allowOptionalFailures"] as? Bool,
                  let logging = config["logging"] as? Bool,
                  Set(disabled).count == disabled.count,
                  disabled.allSatisfy({ name in Self.optionalChecks.contains { $0.key == name } }) else {
                self.settingsMessage = "读取检查设置失败，请恢复默认后重试"
                return
            }
            self.disabledChecks = Set(disabled)
            self.allowOptionalFailures = allow
            self.loggingEnabled = logging
            self.settingsMessage = ""
        }
    }

    func saveCheckConfig() {
        guard !isBusy, !settingsBusy, coreAvailable() else { return }
        let allowed = Set(Self.optionalChecks.map { $0.key })
        guard disabledChecks.isSubset(of: allowed) else {
            settingsMessage = "包含不允许关闭的安全检查"
            return
        }
        let config: [String: Any] = [
            "disabled": Self.optionalChecks.map { $0.key }.filter { disabledChecks.contains($0) },
            "allowOptionalFailures": allowOptionalFailures,
            "logging": loggingEnabled,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: config, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else { return }
        settingsBusy = true
        settingsMessage = "正在保存…"
        run(["set-check-config", json]) { result in
            self.settingsBusy = false
            self.settingsMessage = result.succeeded ? "设置已保存，正在重新检查" : result.output
            if result.succeeded { self.refresh() }
        }
    }

    func restoreCheckDefaults() {
        disabledChecks = []
        allowOptionalFailures = false
        loggingEnabled = true
        settingsMessage = "已恢复默认选项，点击保存后生效"
    }

    func exportDiagnostics() {
        guard !isBusy, !settingsBusy, coreAvailable() else { return }
        let panel = NSSavePanel()
        panel.title = "导出脱敏诊断日志"
        panel.message = "仅包含检查状态、设置和脱敏操作日志，不含 IP、凭据、账号或代理配置。"
        panel.nameFieldStringValue = "claude-switcher-diagnostics.zip"
        panel.allowedContentTypes = [.zip]
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        settingsBusy = true
        settingsMessage = "正在导出诊断日志…"
        run(["export-diagnostics", destination.path], timeout: operationTimeout) { result in
            self.settingsBusy = false
            let message = result.succeeded ? result.output : "导出失败：\(result.output)"
            self.settingsMessage = message
            let alert = NSAlert()
            alert.messageText = result.succeeded ? "诊断日志已导出" : "诊断日志导出失败"
            alert.informativeText = message
            alert.alertStyle = result.succeeded ? .informational : .warning
            alert.runModal()
        }
    }

    func setClient(_ key: String) {
        guard !isBusy, !isSwitchingClient else { return }
        guard Self.clientChoices.contains(where: { $0.key == key }) else { return }
        guard key != currentClient else { return }
        guard coreAvailable() else { markCoreMissing(); return }
        isBusy = true
        isSwitchingClient = true
        banner = "正在切换代理客户端…"
        run(["set-client", key], timeout: operationTimeout) { result in
            self.isSwitchingClient = false
            guard result.succeeded else {
                self.banner = result.output
                self.isBusy = false
                return
            }
            // 切换后重新完成环境审计,刷新「代理内核」等检查项
            self.currentClient = key
            self.checkEnvironment { _ in self.isBusy = false }
        }
    }

    func fixAll() {
        guard !isBusy else { return }
        guard coreAvailable() else { markCoreMissing(); return }
        hideAfterSwitch?.cancel()
        isBusy = true
        phase = .fixing
        banner = "正在修复…（若弹出密码框请输入开机密码）"
        run(["fix"], timeout: operationTimeout) { result in
            guard result.succeeded else {
                self.banner = result.output; self.phase = .failed; self.isBusy = false
                return
            }
            self.checkEnvironment { _ in self.isBusy = false }
        }
    }

    var bundledInstallerURL: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let installer = resources.appendingPathComponent("InstallerSource/installer/安装.command")
        return FileManager.default.isReadableFile(atPath: installer.path) ? installer : nil
    }

    var needsComponentInstallation: Bool {
        if coreMissing { return true }
        guard let resources = Bundle.main.resourceURL,
              let bundled = try? String(contentsOf: resources.appendingPathComponent("InstallerSource/VERSION"), encoding: .utf8) else { return false }
        let installed = (try? String(contentsOfFile: NSHomeDirectory() + "/.claude-switcher/version.txt", encoding: .utf8)) ?? ""
        return bundled.trimmingCharacters(in: .whitespacesAndNewlines) != installed.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func openBundledInstaller() {
        guard !isBusy, !settingsBusy else { return }
        if let installer = bundledInstallerURL {
            if !NSWorkspace.shared.open(installer) { banner = "无法打开安装器，请从安装包中的 installer/安装.command 手动打开。" }
        } else if let url = URL(string: "https://github.com/parafallen-maker/claude-network-rules/releases/latest") {
            NSWorkspace.shared.open(url)
        }
    }

    var environmentSettings: [String: Any]? {
        let group = environmentGroup.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !group.isEmpty, group.utf8.count <= 160,
              let port = Int(browserPort), (1024...65535).contains(port) else { return nil }
        return ["proxy_group": group, "browser_port": port, "configure_dns": configureDNS,
                "auto_group": automaticGroup, "enable_browser_proxy": enableBrowserProxy]
    }

    private var currentEnvironmentSignature: String {
        guard let settings = environmentSettings,
              let data = try? JSONSerialization.data(withJSONObject: settings, options: [.sortedKeys]) else { return "" }
        return String(decoding: data, as: UTF8.self)
    }

    var hasEnvironmentPlan: Bool {
        !environmentPlanSignature.isEmpty && environmentPlanSignature == currentEnvironmentSignature
    }

    private func configurationResult(_ result: CoreResult, secret: String = "") -> (object: [String: Any], message: String, tone: ConfigurationTone) {
        guard let data = result.output.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            // 不将未知的原始响应展示或记录，避免第三方接口回显凭据。
            let message = result.output.hasPrefix("操作超时")
                ? "操作超时，请重新读取配置后重试。"
                : "操作未完成：核心组件未返回有效结果。请确认本机组件与 App 版本一致后重试。"
            return ([:], message, .failure)
        }
        var message = object["message"] as? String ?? (result.succeeded ? "操作完成" : "操作未完成")
        if let manual = object["manual_required"] as? [String], !manual.isEmpty {
            message += "\n" + manual.joined(separator: "\n")
        }
        if !secret.isEmpty { message = message.replacingOccurrences(of: secret, with: "[已隐藏]") }
        message = String(message.prefix(5000))
        let succeeded = result.succeeded && object["ok"] as? Bool == true
        let partial = object["partial"] as? Bool == true
            || !(object["manual_required"] as? [String] ?? []).isEmpty
            || (succeeded && object["complete"] as? Bool == false)
        let tone: ConfigurationTone = partial ? .warning : (succeeded ? .success : .failure)
        return (object, message, tone)
    }

    private func acceptEnvironmentState(_ object: [String: Any]) {
        if let settings = object["settings"] as? [String: Any] {
            if let value = settings["proxy_group"] as? String { environmentGroup = value }
            if let value = settings["browser_port"] as? Int { browserPort = String(value) }
            if let value = settings["configure_dns"] as? Bool { configureDNS = value }
            if let value = settings["auto_group"] as? Bool { automaticGroup = value }
            if let value = settings["enable_browser_proxy"] as? Bool { enableBrowserProxy = value }
        }
        acceptRegionState(object)
    }

    private func acceptRegionState(_ object: [String: Any]) {
        if let region = object["region"] as? [String: Any] {
            let parts = [region["country"], region["timezone"], region["locale"]].compactMap { $0 as? String }.filter { !$0.isEmpty }
            if !parts.isEmpty { regionSummary = parts.joined(separator: " · ") }
        }
    }

    func environmentCommand(_ action: String) {
        guard ["status", "plan", "apply", "restore"].contains(action), !isBusy, !settingsBusy else { return }
        guard coreAvailable(), !needsComponentInstallation else {
            environmentMessage = "请先点击主界面的“安装本机组件”，完成后重新检查。"
            environmentTone = .failure
            return
        }
        let settings = environmentSettings
        if ["plan", "apply"].contains(action), settings == nil {
            environmentMessage = "请输入代理组名称，浏览器端口需在 1024–65535 之间。"
            environmentTone = .failure
            return
        }
        if action == "apply", !hasEnvironmentPlan {
            environmentMessage = "配置有变化，请先预览本次修改。"
            environmentTone = .warning
            return
        }
        guard let input = try? JSONSerialization.data(withJSONObject: settings ?? [:], options: [.sortedKeys]), input.count <= 16384 else { return }
        let signature = currentEnvironmentSignature
        isBusy = true
        environmentBusy = true
        environmentTone = .neutral
        environmentMessage = action == "status" ? "正在读取配置…" : "正在处理配置…"
        if action != "apply" { environmentPlanSignature = "" }
        run(["environment-config", action], timeout: operationTimeout, input: input, structuredOutput: true) { result in
            let parsed = self.configurationResult(result)
            self.environmentMessage = parsed.message
            self.environmentTone = parsed.tone
            self.environmentChanges = (parsed.object["changes"] as? [String] ?? []).map { String($0.prefix(600)) }
            if action == "status" || action == "restore" { self.acceptEnvironmentState(parsed.object) }
            if action == "status" { self.environmentLoaded = result.succeeded && parsed.object["ok"] as? Bool == true }
            if action == "plan", result.succeeded, parsed.object["ok"] as? Bool == true { self.environmentPlanSignature = signature }
            if action == "apply" { self.environmentPlanSignature = "" }
            self.environmentBusy = false
            self.isBusy = false
        }
    }

    func configureSystem(detectRegion: Bool) {
        guard !isBusy, !settingsBusy, coreAvailable(), !needsComponentInstallation else { return }
        isBusy = true
        environmentBusy = true
        environmentTone = .neutral
        environmentMessage = detectRegion ? "正在识别出口地区并应用系统设置；请完成管理员验证…" : "正在修复环境；请完成管理员验证…"
        run([detectRegion ? "set-region" : "fix"], timeout: operationTimeout) { result in
            guard result.succeeded else {
                self.environmentMessage = "系统配置未完成，请查看主界面结果并重新检查。"
                self.environmentTone = .failure
                self.environmentBusy = false
                self.isBusy = false
                return
            }
            self.checkEnvironment { passed in
                self.run(["environment-config", "status"], timeout: self.checkTimeout, input: Data("{}".utf8), structuredOutput: true) { status in
                    let parsed = self.configurationResult(status)
                    self.acceptRegionState(parsed.object)
                    self.environmentMessage = passed ? "系统配置已应用，环境检查已通过。" : "系统配置已应用，但环境检查尚未全部通过，请查看主界面。"
                    self.environmentTone = passed ? .success : .warning
                    self.environmentBusy = false
                    self.isBusy = false
                }
            }
        }
    }

    func browserCommand(_ action: String, endpoint: String, apiKey: String, profileID: String, page: Int = 1) {
        guard ["list", "configure", "restore"].contains(action), !isBusy, !settingsBusy else { return }
        guard coreAvailable(), !needsComponentInstallation else {
            browserMessage = "请先安装随包本机组件，再连接指纹浏览器。"
            browserTone = .failure
            return
        }
        guard let url = URL(string: endpoint), url.scheme == "http",
              ["127.0.0.1", "localhost", "::1", "[::1]"].contains(url.host ?? ""),
              url.user == nil, url.password == nil,
              endpoint.utf8.count <= 2048, apiKey.utf8.count <= 4096,
              profileID.utf8.count <= 256 else {
            browserMessage = "请使用本机 AdsPower Local API 地址，例如 http://127.0.0.1:50325。"
            browserTone = .failure
            return
        }
        guard action == "list" || !profileID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            browserMessage = "请先选择或填写要配置的浏览器环境 ID。"
            browserTone = .failure
            return
        }
        guard let port = Int(browserPort), (1024...65535).contains(port) else {
            browserMessage = "请在网络页填写有效的浏览器专用端口。"
            browserTone = .failure
            return
        }
        let payload: [String: Any] = ["endpoint": endpoint, "api_key": apiKey, "profile_id": profileID, "port": port, "page": max(1, page)]
        guard let input = try? JSONSerialization.data(withJSONObject: payload), input.count <= 16384 else { return }
        isBusy = true
        browserBusy = true
        browserTone = .neutral
        browserMessage = "正在连接本机 AdsPower…"
        run(["browser-proxy", action], timeout: operationTimeout, input: input, structuredOutput: true) { result in
            let parsed = self.configurationResult(result, secret: apiKey)
            self.browserMessage = parsed.message
            self.browserTone = parsed.tone
            if action == "list" {
                let profiles: [BrowserProfile] = (parsed.object["profiles"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let id = item["id"] as? String ?? item["profile_id"] as? String ?? item["user_id"] as? String else { return nil }
                    let name = String((item["name"] as? String ?? id).prefix(160))
                    return BrowserProfile(id: id, name: name)
                }
                if page > 1 {
                    let known = Set(self.browserProfiles.map { $0.id })
                    self.browserProfiles += profiles.filter { !known.contains($0.id) }
                } else { self.browserProfiles = profiles }
                self.browserHasMore = parsed.object["has_more"] as? Bool == true
                self.browserPage = parsed.object["page"] as? Int ?? page
            }
            self.browserBusy = false
            self.isBusy = false
        }
    }

    func inspectFingerprint() {
        guard !isBusy else { return }
        isBusy = true
        fingerprint = "正在体检…"
        run(["fingerprint"], timeout: 90) { result in
            self.fingerprint = result.output
            self.isBusy = false
        }
    }

    func checkUpdate() {
        guard !checkingUpdate else { return }
        checkingUpdate = true
        updateText = "正在检查更新…"
        run(["update-check"], timeout: 120) { result in
            self.updateText = result.output.isEmpty
                ? (result.succeeded ? "检查完成，核心脚本没有返回结果" : "检查失败，请重试")
                : result.output
            self.checkingUpdate = false
        }
    }

    var aiPrompt: String {
        let home = NSHomeDirectory()
        let agents = home + "/.claude-switcher/AGENTS.md"
        let agentsLine = FileManager.default.fileExists(atPath: agents)
            ? "本机交接文档：\(agents)"
            : "本机交接文档：\(agents)（~/.claude-switcher 不存在或其中没有 AGENTS.md，请从仓库根目录读取 AGENTS.md）"
        return """
        这是「Claude 切换器」的 AI 交接提示词。请先完整阅读交接文档，再开始安装、配置或排障。

        仓库地址：https://github.com/parafallen-maker/claude-network-rules
        \(agentsLine)

        执行要求：
        1. 先读 AGENTS.md，按其中的安装步骤、验收标准与安全红线执行，不要只凭本段提示词猜测。
        2. 安装或排障时先跑环境审计：硬性安全项必须通过；只有显式配置的非关键项可跳过或降为警告，skip 不表示检查通过。
        3. 账号切换前先完全退出 Claude；账号快照按 AGENTS.md 的规则保留，不要改动 ~/.claude-switcher/accounts/ 的结构。
        4. 保镖等常驻脚本只放在 root 所有的系统目录，不要改回用户可写路径；需要变更时引导我重跑安装器。
        5. 不要未经我明确同意更新、覆盖或重装已安装版本（~/.local/bin、桌面 App、launchd 与系统目录），也不要执行 update 或安装器；先说明影响并征得同意。
        6. 检查新版本只运行 `~/.local/bin/claude-switch.sh update-check`，看到结果后先问我是否执行更新。
        7. 不要在日志、文档或回复里暴露我的出口 IP、代理地址或任何凭据。
        """
    }

    func switchTo(_ target: String) {
        guard !isBusy, Self.accountLetters.contains(target) else { return }
        hideAfterSwitch?.cancel()
        isBusy = true
        switchRequested = true
        // 常驻后旧检查可能已经过期。每次切换都重新审计，不使用落盘的旧结果放行。
        checkEnvironment { passed in
            guard passed else {
                self.switchRequested = false; self.isBusy = false
                return
            }
            self.performSwitch(target)
        }
    }

    private func performSwitch(_ target: String) {
        phase = .switching
        banner = "正在切换到账号 \(target)…"
        run(["switch", target], timeout: operationTimeout) { [self] result in
            let lines = result.output.split(separator: "\n").map(String.init)
            let failure = lines.first { $0.hasPrefix("FAIL:") }
            let success = lines.contains { $0.hasPrefix("OK:") || $0.hasPrefix("INFO:") }
            guard result.succeeded, failure == nil, success else {
                self.phase = .failed
                self.banner = failure.map { String($0.dropFirst(5)) } ?? (result.output.isEmpty ? "切换未返回成功结果，请重新检查" : result.output)
                self.switchRequested = false; self.isBusy = false
                return
            }
            var extra = ""
            if target != "A" && !self.archived.contains(target) {
                extra = "\(target) 号首次使用：请在打开的 Claude 里登录 \(target) 账号，登录一次即可。"
            }
            let unchanged = lines.contains { $0.hasPrefix("INFO:") }
            if unchanged { extra = "" }
            self.current = target
            self.phase = .done
            self.banner = unchanged ? "当前已是账号 \(target)" : "已切换到账号 \(target)"
            self.note = extra
            self.switchRequested = false; self.isBusy = false
            let hide = DispatchWorkItem { [weak self] in
                guard let self = self, self.phase == .done, !self.isBusy else { return }
                self.onSwitchCompleted?()
                self.phase = .ready
            }
            self.hideAfterSwitch = hide
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.2, execute: hide)
        }
    }
}

// MARK: - Claude 风格外观

enum ClaudeTheme {
    // 纸感暖色基底 (Warm Parchment / Alabaster)
    static let bgTop = Color(red: 0.984, green: 0.976, blue: 0.961)         // #FAF9F5
    static let bgBottom = Color(red: 0.957, green: 0.945, blue: 0.925)      // #F4F1EC
    static let card = Color.white                                           // #FFFFFF
    static let cardBorder = Color(red: 0.898, green: 0.882, blue: 0.855)   // #E5E1DA
    static let divider = Color(red: 0.941, green: 0.929, blue: 0.910)      // #F0ECE8
    static let iconWell = Color(red: 0.965, green: 0.953, blue: 0.937)     // #F6F3EF

    // 字体色阶 (Editorial Ink & Warm Stone)
    static let ink = Color(red: 0.165, green: 0.153, blue: 0.141)          // #2A2724
    static let muted = Color(red: 0.455, green: 0.435, blue: 0.412)        // #746F69

    // Claude 标志性陶土橙 (Anthropic Claude Terracotta)
    static let terracotta = Color(red: 0.847, green: 0.447, blue: 0.329)   // #D87254
    static let terracottaDeep = Color(red: 0.761, green: 0.369, blue: 0.259) // #C25E42
    static let terracottaTint = Color(red: 0.847, green: 0.447, blue: 0.329).opacity(0.09)

    // 自然柔和状态色
    static let success = Color(red: 0.180, green: 0.490, blue: 0.322)       // #2E7D52 (Sage Green)

    static let warning = Color(red: 0.722, green: 0.431, blue: 0.094)       // #B86E18 (Warm Amber)
    static let warningBorder = Color(red: 0.961, green: 0.863, blue: 0.718) // #F5DCB7

    static let error = Color(red: 0.761, green: 0.282, blue: 0.212)         // #C24836 (Claude Terracotta Red)
}

// MARK: - Claude 优雅背景

struct ClaudeBackground: View {
    var body: some View {
        ZStack {
            LinearGradient(
                colors: [ClaudeTheme.bgTop, ClaudeTheme.bgBottom],
                startPoint: .top,
                endPoint: .bottom
            )
            RadialGradient(
                colors: [ClaudeTheme.terracotta.opacity(0.04), Color.clear],
                center: .top,
                startRadius: 0,
                endRadius: 400
            )
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

// MARK: - 辅助样式组件

struct ClaudeIconButton: View {
    let systemName: String
    let help: String
    var disabled: Bool = false
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 13, weight: .medium))
                .foregroundColor(isHovered ? ClaudeTheme.ink : ClaudeTheme.muted)
                .frame(width: 28, height: 28)
                .background(isHovered ? ClaudeTheme.iconWell : ClaudeTheme.card.opacity(0.85))
                .clipShape(RoundedRectangle(cornerRadius: 7.5))
                .overlay(
                    RoundedRectangle(cornerRadius: 7.5)
                        .stroke(isHovered ? ClaudeTheme.cardBorder : ClaudeTheme.divider, lineWidth: 1)
                )
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .accessibilityLabel(help)
        .help(help)
        .onHover { isHovered = $0 }
    }
}

// MARK: - 视图

struct ContentView: View {
    @ObservedObject var vm: VM
    var height: CGFloat = 900
    @State private var showNotice = false
    @State private var showFingerprint = false
    @State private var showAIPrompt = false

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(spacing: 12) {
                header
                HStack {
                    Text("环境检查").font(.caption.weight(.semibold)).foregroundColor(ClaudeTheme.muted)
                    Spacer()
                    statusPill
                }
                // 小屏上窗口高度会被系统可见区域压缩,让卡片区可滚动而不是被裁切
                ScrollView {
                    VStack(spacing: 16) {
                        checksCard
                        clientCard
                        environmentConfigurationButton
                        if vm.needsComponentInstallation && !vm.coreMissing { setupCard }
                        if vm.phase == .ready || vm.phase == .switching || vm.phase == .done {
                            accountCard
                        } else if vm.coreMissing {
                            setupCard
                        } else if vm.phase == .failed || vm.phase == .fixing {
                            retryCard
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                footer
            }
            .padding(16)
        }
        .sheet(isPresented: $showAIPrompt) { AIPromptSheet(text: vm.aiPrompt) }
        .sheet(isPresented: $vm.showSettingsSheet) { CheckSettingsView(vm: vm) }
        .sheet(isPresented: $vm.showEnvironmentSheet) { EnvironmentConfigurationView(vm: vm) }
        .frame(width: 440, height: height)
        .preferredColorScheme(.light)
    }

    var header: some View {
        HStack(spacing: 11) {
            // Claude 风格陶土盾牌徽标
            ZStack {
                RoundedRectangle(cornerRadius: 10)
                    .fill(
                        LinearGradient(
                            colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                    .frame(width: 40, height: 40)
                    .overlay(
                        RoundedRectangle(cornerRadius: 10)
                            .stroke(Color.white.opacity(0.22), lineWidth: 1)
                    )
                    .shadow(color: ClaudeTheme.terracotta.opacity(0.24), radius: 3.5, x: 0, y: 1.5)

                Image(systemName: "shield.lefthalf.filled")
                    .font(.system(size: 19, weight: .medium))
                    .foregroundColor(.white)
            }

            VStack(alignment: .leading, spacing: 2) {
                Text("Claude 账号切换器")
                    .font(.system(size: 16, weight: .bold, design: .serif))
                    .foregroundColor(ClaudeTheme.ink)
                Text("安全检查 · 切换 · 启动")
                    .font(.system(size: 11, weight: .regular))
                    .foregroundColor(ClaudeTheme.muted)
            }

            Spacer()

            ClaudeIconButton(
                systemName: "waveform.path.ecg",
                help: "设备指纹体检(字体/GPU/虚拟机)",
                disabled: vm.isBusy
            ) {
                showFingerprint = true
                vm.inspectFingerprint()
            }

            ClaudeIconButton(
                systemName: "gearshape",
                help: "检查设置与诊断日志",
                disabled: vm.isBusy || vm.settingsBusy
            ) {
                vm.showSettingsSheet = true
            }

            ClaudeIconButton(
                systemName: "info.circle",
                help: "安全防护体系公告"
            ) {
                showNotice = true
            }

        }
        .sheet(isPresented: $showNotice) { NoticeView() }
        .sheet(isPresented: $showFingerprint) { FingerprintView(text: vm.fingerprint) }
    }

    var statusPill: some View {
        let (txt, color): (String, Color) = {
            switch vm.phase {
            case .checking: return ("检查中", ClaudeTheme.terracottaDeep)
            case .ready: return ("环境安全", ClaudeTheme.success)
            case .failed: return ("检查未过", ClaudeTheme.error)
            case .switching: return ("切换中", ClaudeTheme.terracottaDeep)
            case .fixing: return ("修复中", ClaudeTheme.terracottaDeep)
            case .done: return ("完成", ClaudeTheme.success)
            }
        }()
        return Text(txt)
            .font(.caption.bold())
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(color.opacity(0.18))
            .foregroundColor(color)
            .clipShape(Capsule())
    }

    var checksCard: some View {
        VStack(spacing: 0) {
            ForEach(Array(vm.rows.enumerated()), id: \.element.id) { idx, row in
                HStack(spacing: 10) {
                    // 左侧图标容器
                    ZStack {
                        RoundedRectangle(cornerRadius: 6)
                            .fill(ClaudeTheme.iconWell)
                            .frame(width: 24, height: 24)
                            .overlay(
                                RoundedRectangle(cornerRadius: 6)
                                    .stroke(ClaudeTheme.divider, lineWidth: 0.5)
                            )
                        Image(systemName: row.icon)
                            .font(.system(size: 11.5, weight: .medium))
                            .foregroundColor(ClaudeTheme.muted)
                    }

                    Text(row.name)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundColor(ClaudeTheme.ink)

                    Spacer(minLength: 8)

                    if !row.detail.isEmpty {
                        Text(row.detail)
                            .font(.system(size: 11.5, weight: .regular, design: isMonospace(row.detail) ? .monospaced : .default))
                            .foregroundColor(row.state == .warn ? ClaudeTheme.warning : ClaudeTheme.muted)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }

                    stateIcon(row.state)
                }
                .padding(.vertical, 5.5)
                .padding(.horizontal, 12)

                if idx < vm.rows.count - 1 {
                    Divider()
                        .overlay(ClaudeTheme.divider)
                        .padding(.leading, 46)
                }
            }
        }
        .background(ClaudeTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 13))
        .overlay(
            RoundedRectangle(cornerRadius: 13)
                .stroke(ClaudeTheme.cardBorder, lineWidth: 1)
        )
        .shadow(color: Color(red: 0.2, green: 0.16, blue: 0.12).opacity(0.035), radius: 6, x: 0, y: 2)
    }

    private func isMonospace(_ str: String) -> Bool {
        str.contains(".") || str.contains(":") || str.contains("/")
    }

    var clientCard: some View {
        HStack(spacing: 10) {
            ZStack {
                RoundedRectangle(cornerRadius: 6)
                    .fill(ClaudeTheme.iconWell)
                    .frame(width: 24, height: 24)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(ClaudeTheme.divider, lineWidth: 0.5))
                Image(systemName: "network")
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundColor(ClaudeTheme.muted)
            }
            Text("代理客户端").font(.system(size: 13, weight: .medium)).foregroundColor(ClaudeTheme.ink)
            Spacer()
            if vm.isSwitchingClient {
                ProgressView().scaleEffect(0.6).frame(width: 16, height: 16)
            }
            Picker("", selection: Binding(
                get: { vm.currentClient },
                set: { vm.setClient($0) }
            )) {
                ForEach(VM.clientChoices, id: \.key) { choice in
                    Text(choice.label).tag(choice.key)
                }
            }
            .labelsHidden()
            .frame(width: 150)
            .disabled(vm.isBusy || vm.isSwitchingClient)
        }
        .padding(.vertical, 7)
        .padding(.horizontal, 12)
        .background(ClaudeTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13).stroke(ClaudeTheme.cardBorder, lineWidth: 1))
        .shadow(color: Color(red: 0.2, green: 0.16, blue: 0.12).opacity(0.035), radius: 6, x: 0, y: 2)
    }

    func stateIcon(_ s: RowState) -> some View {
        Group {
            switch s {
            case .pending: ProgressView().scaleEffect(0.55).frame(width: 16, height: 16)
            case .ok: Image(systemName: "checkmark.circle.fill").foregroundColor(ClaudeTheme.success)
            case .warn: Image(systemName: "exclamationmark.triangle.fill").foregroundColor(ClaudeTheme.warning)
            case .fail: Image(systemName: "xmark.octagon.fill").foregroundColor(ClaudeTheme.error)
            case .skip: Image(systemName: "minus.circle").foregroundColor(ClaudeTheme.muted)
            }
        }
        .font(.callout)
    }

    var accountCard: some View {
        VStack(spacing: 12) {
            if vm.phase == .switching {
                HStack(spacing: 8) {
                    ProgressView().scaleEffect(0.7)
                    Text(vm.banner).font(.callout).foregroundColor(ClaudeTheme.ink)
                }
                .frame(maxWidth: .infinity).padding(.vertical, 10)
            } else if vm.phase == .done {
                VStack(spacing: 4) {
                    Label(vm.banner, systemImage: "checkmark.circle.fill").foregroundColor(ClaudeTheme.success).font(.callout.bold())
                    if !vm.note.isEmpty { Text(vm.note).font(.caption).foregroundColor(ClaudeTheme.warning).multilineTextAlignment(.center) }
                }
                .frame(maxWidth: .infinity).padding(.vertical, 10)
            } else {
                HStack(spacing: 6) {
                    Text("当前账号").font(.caption).foregroundColor(ClaudeTheme.muted)
                    Text(vm.current).font(.caption.bold())
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(ClaudeTheme.terracottaTint).foregroundColor(ClaudeTheme.terracottaDeep)
                        .clipShape(Capsule())
                    Spacer()
                }
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                    ForEach(VM.accountLetters, id: \.self) { t in
                        accountButton(t, subtitle: accountSubtitle(t))
                    }
                }
            }
        }
        .padding(12)
        .background(ClaudeTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(ClaudeTheme.cardBorder))
    }

    func accountSubtitle(_ t: String) -> String {
        if vm.current == t { return "当前" }
        guard vm.archived.contains(t) else { return "未初始化" }
        guard let stamp = vm.stamps[t] else { return "已建档" }
        return "已建档 · \(stamp.size) · \(shortDate(stamp.date))"
    }

    func shortDate(_ value: String) -> String {
        let parts = value.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].count == 4 else { return value }
        return "\(parts[1])-\(parts[2])"
    }

    func accountButton(_ t: String, subtitle: String) -> some View {
        Button { vm.switchTo(t) } label: {
            VStack(spacing: 4) {
                Text("账号 \(t)").font(.headline).foregroundColor(vm.current == t ? .white : ClaudeTheme.ink)
                Text(subtitle).font(.caption2).foregroundColor(vm.current == t ? .white.opacity(0.9) : ClaudeTheme.muted)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 14)
            .background(
                vm.current == t
                ? LinearGradient(colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep], startPoint: .top, endPoint: .bottom)
                : LinearGradient(colors: [ClaudeTheme.iconWell, ClaudeTheme.iconWell], startPoint: .top, endPoint: .bottom)
            )
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(vm.current == t ? ClaudeTheme.terracotta : ClaudeTheme.cardBorder, lineWidth: 1.2))
        }
        .buttonStyle(.plain)
        .disabled(vm.isBusy)
    }

    var environmentConfigurationButton: some View {
        Button { vm.showEnvironmentSheet = true } label: {
            HStack(spacing: 10) {
                Image(systemName: "slider.horizontal.3").foregroundColor(ClaudeTheme.terracottaDeep)
                VStack(alignment: .leading, spacing: 3) {
                    Text("环境配置").font(.callout.bold()).foregroundColor(ClaudeTheme.ink)
                    Text("分流与 DNS · 系统地区 · 指纹浏览器").font(.caption2).foregroundColor(ClaudeTheme.muted)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.caption).foregroundColor(ClaudeTheme.muted)
            }
            .padding(12).background(ClaudeTheme.card)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(ClaudeTheme.cardBorder))
        }
        .buttonStyle(.plain)
        .disabled(vm.isBusy || vm.settingsBusy)
    }

    var setupCard: some View {
        VStack(spacing: 10) {
            Text(vm.coreMissing ? "还差一步：安装本机组件" : "随包组件有更新")
                .font(.callout.bold()).foregroundColor(ClaudeTheme.ink)
            Text("完整安装会更新本机脚本与保护服务。请在终端完成管理员验证，再返回重新检查。")
                .font(.caption).foregroundColor(ClaudeTheme.muted).multilineTextAlignment(.center)
            HStack(spacing: 10) {
                Button(vm.bundledInstallerURL == nil ? "打开安装下载页" : "安装本机组件") { vm.openBundledInstaller() }
                    .buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
                Button("完成安装后重新检查") { vm.refresh() }.disabled(vm.isBusy)
            }
        }
        .padding(12).frame(maxWidth: .infinity)
        .background(ClaudeTheme.terracottaTint)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(ClaudeTheme.cardBorder))
    }

    private func openURL(_ text: String) {
        if let url = URL(string: text) { NSWorkspace.shared.open(url) }
    }

    var retryCard: some View {
        VStack(spacing: 10) {
            if vm.phase == .fixing {
                HStack(spacing: 8) {
                    ProgressView().scaleEffect(0.7)
                    Text(vm.banner).font(.callout).foregroundColor(ClaudeTheme.ink)
                }
            } else {
                if !vm.banner.isEmpty {
                    Text(vm.banner).font(.callout).foregroundColor(ClaudeTheme.error).multilineTextAlignment(.center)
                }
                Text("存在未通过项，可一键修复或手动处理后重试").font(.caption).foregroundColor(ClaudeTheme.muted)
                HStack(spacing: 10) {
                    Button { vm.fixAll() } label: {
                        Label("一键修复", systemImage: "wand.and.stars")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 8)
                            .background(LinearGradient(colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep], startPoint: .leading, endPoint: .trailing))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(vm.isBusy)
                    Button { vm.refresh() } label: {
                        Label("重新检查", systemImage: "arrow.clockwise")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 8)
                            .background(ClaudeTheme.iconWell)
                            .foregroundColor(ClaudeTheme.ink)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(vm.isBusy)
                }
            }
        }
        .frame(maxWidth: .infinity)
        .padding(12)
        .background(ClaudeTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(ClaudeTheme.warningBorder))
    }

    var footer: some View {
        HStack(spacing: 10) {
            Image(systemName: "lock.shield").font(.caption2)
            Text("硬性安全项必须通过；灰色跳过不表示检查通过").font(.caption2)
            Spacer()
            Button {
                vm.checkUpdate()
                vm.showUpdateSheet = true
            } label: {
                Text("检查更新").font(.caption2).foregroundColor(ClaudeTheme.muted)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("检查更新")
            Button { showAIPrompt = true } label: {
                Text("AI 配置").font(.caption2).foregroundColor(ClaudeTheme.muted)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("AI 配置")
        }
        .foregroundColor(ClaudeTheme.muted)
        .sheet(isPresented: $vm.showUpdateSheet) { UpdateSheet(vm: vm) }
    }
}

// MARK: - 检查设置与诊断

struct CheckSettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var vm: VM
    @State private var showEnvironment = false

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("检查设置").font(.title3.bold()).foregroundColor(ClaudeTheme.ink)
                        Text("安全项始终强制执行；跳过不等于通过").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(ClaudeTheme.muted)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("关闭")
                }

                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        GroupBox("环境配置") {
                            HStack {
                                Text("设置分流、DNS、系统地区与指纹浏览器").font(.caption).foregroundColor(ClaudeTheme.muted)
                                Spacer()
                                Button("打开配置") { showEnvironment = true }
                                    .disabled(vm.isBusy || vm.settingsBusy)
                            }.padding(.vertical, 6)
                        }
                        GroupBox("STUN 网络策略") {
                            VStack(alignment: .leading, spacing: 10) {
                                Toggle("阻断常见 STUN UDP 端口", isOn: $vm.blockSTUN)
                                Text(vm.blockSTUN
                                     ? "开启：阻断 UDP 3478、5349、19302 并验证规则生效，可能影响部分音视频通话。"
                                     : "关闭：STUN 经代理转发，需要节点实际支持 UDP；无响应时仍会检查失败。")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
                                Text("此开关不关闭 WebRTC 检查。应用会重启代理并重新检查。Clash Verge 使用规则模式：Claude 和 IP 检测优先代理，国内及局域网直连，其余流量代理；全局模式会绕过分流和 STUN 防护。")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
                                Button("应用 STUN 设置并检查") { vm.applySTUNPolicy() }
                                    .disabled(!vm.stunPolicyLoaded || !["clash-verge", "flclash"].contains(vm.currentClient))
                                if !vm.stunPolicyMessage.isEmpty {
                                    Text(vm.stunPolicyMessage).font(.caption)
                                        .foregroundColor(ClaudeTheme.muted)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            .padding(.vertical, 6)
                        }
                        .disabled(vm.settingsBusy || vm.isBusy)

                        GroupBox("可自定义的非关键检查") {
                            VStack(alignment: .leading, spacing: 10) {
                                ForEach(VM.optionalChecks, id: \.key) { item in
                                    Toggle(item.label, isOn: Binding(
                                        get: { !vm.disabledChecks.contains(item.key) },
                                        set: { enabled in
                                            if enabled { vm.disabledChecks.remove(item.key) }
                                            else { vm.disabledChecks.insert(item.key) }
                                        }
                                    ))
                                }
                                Text("关闭后会真正跳过对应探测，并以灰色“已跳过”显示。出口国家、代理/TUN、Kill-Switch、保镖、账号归档和更新校验不可关闭。")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
                            }
                            .padding(.vertical, 6)
                        }

                        GroupBox("失败处理与日志") {
                            VStack(alignment: .leading, spacing: 10) {
                                Toggle("将上述非关键项的失败降为警告", isOn: $vm.allowOptionalFailures)
                                Text("只影响这 6 项；不会放宽任何硬性安全项，也不会自动关闭检测。")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
                                Toggle("记录脱敏客户端日志", isOn: $vm.loggingEnabled)
                                Text("仅记录版本、命令、退出码、耗时和检查状态，不记录 IP、凭据或账号内容。")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
                            }
                            .padding(.vertical, 6)
                        }

                        GroupBox("问题排查") {
                            VStack(alignment: .leading, spacing: 9) {
                                Button("导出脱敏诊断日志…") { vm.exportDiagnostics() }
                                    .disabled(vm.settingsBusy)
                                Text("日志：~/Library/Logs/claude-switcher/client.log")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted).textSelection(.enabled)
                            }
                            .padding(.vertical, 6)
                        }
                    }
                }

                if !vm.settingsMessage.isEmpty {
                    Text(vm.settingsMessage).font(.caption).foregroundColor(ClaudeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack {
                    Button("恢复默认") { vm.restoreCheckDefaults() }.disabled(vm.settingsBusy)
                    Spacer()
                    if vm.settingsBusy { ProgressView().scaleEffect(0.65) }
                    Button("保存并重新检查") { vm.saveCheckConfig() }
                        .buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
                        .disabled(vm.settingsBusy || vm.isBusy)
                }
            }
            .padding(20)
        }
        .frame(width: 520, height: 610)
        .preferredColorScheme(.light)
        .onAppear { vm.loadCheckConfig() }
        .sheet(isPresented: $showEnvironment) { EnvironmentConfigurationView(vm: vm) }
    }
}

// MARK: - 环境配置

struct EnvironmentConfigurationView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var vm: VM
    @State private var tab = 0
    @State private var browserEndpoint = "http://127.0.0.1:50325"
    @State private var browserAPIKey = ""
    @State private var browserProfileID = ""

    init(vm: VM, initialTab: Int = 0) {
        self.vm = vm
        _tab = State(initialValue: initialTab)
    }

    private var busy: Bool { vm.isBusy || vm.settingsBusy }

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(alignment: .leading, spacing: 15) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("环境配置").font(.title2.bold()).foregroundColor(ClaudeTheme.ink)
                        Text("配置、预览与恢复都在这里完成").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Spacer()
                    if vm.environmentBusy || vm.browserBusy { ProgressView().scaleEffect(0.7) }
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(ClaudeTheme.muted)
                    }
                    .buttonStyle(.plain).disabled(vm.environmentBusy || vm.browserBusy)
                    .accessibilityLabel("关闭环境配置")
                }
                Picker("配置类别", selection: $tab) {
                    Text("网络").tag(0)
                    Text("系统").tag(1)
                    Text("指纹浏览器").tag(2)
                }.pickerStyle(.segmented)
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        if vm.needsComponentInstallation {
                            GroupBox("需要安装本机组件") {
                                VStack(alignment: .leading, spacing: 8) {
                                    Text("请打开随包安装器，完成终端中的管理员验证后返回主界面重新检查。")
                                        .font(.caption).foregroundColor(ClaudeTheme.muted)
                                    Button("安装本机组件") { vm.openBundledInstaller() }.disabled(busy)
                                }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 5)
                            }
                        }
                        if tab == 0 { networkSection }
                        else if tab == 1 { systemSection }
                        else { browserSection }
                    }.padding(.vertical, 2)
                }
                if tab != 2 && !vm.environmentMessage.isEmpty {
                    messageBox(vm.environmentMessage, tone: vm.environmentTone)
                }
                if tab == 2 && !vm.browserMessage.isEmpty {
                    messageBox(vm.browserMessage, tone: vm.browserTone)
                }
            }.padding(22)
        }
        .frame(width: 600, height: 690)
        .preferredColorScheme(.light)
        .interactiveDismissDisabled(vm.environmentBusy || vm.browserBusy)
        .onAppear { vm.environmentCommand("status") }
        .onDisappear { browserAPIKey = "" }
    }

    private var networkSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("Clash Verge 分流与 DNS") {
                VStack(alignment: .leading, spacing: 11) {
                    Text("Claude 与 IP 检测走专用代理组，国内与局域网直连，其余流量走代理。")
                        .font(.caption).foregroundColor(ClaudeTheme.muted)
                    HStack {
                        Text("已安装代理组").frame(width: 105, alignment: .leading)
                        Text(vm.environmentLoaded ? vm.environmentGroup : "请先读取当前配置")
                            .fontWeight(.medium).textSelection(.enabled)
                        Spacer()
                    }
                    Text("沿用安装时的代理组。更换组名需运行完整安装器，同步本机脚本与系统保护服务。")
                        .font(.caption2).foregroundColor(ClaudeTheme.muted)
                    Toggle("订阅缺少同名代理组时自动创建", isOn: $vm.automaticGroup)
                    Toggle("应用托管 DNS 设置", isOn: $vm.configureDNS)
                    Text("自动创建的组会先阻断流量。请在 Clash Verge 中选择可用节点，再重新应用并验证。")
                        .font(.caption2).foregroundColor(ClaudeTheme.muted)
                }.padding(.vertical, 6)
            }
            GroupBox("浏览器专用代理") {
                VStack(alignment: .leading, spacing: 11) {
                    Toggle("启用本机专用代理入口", isOn: $vm.enableBrowserProxy)
                    HStack {
                        Text("监听端口").frame(width: 105, alignment: .leading)
                        TextField("7898", text: $vm.browserPort).textFieldStyle(.roundedBorder).frame(width: 110)
                        Text("仅限本机 127.0.0.1").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Text("指纹浏览器可通过这一入口固定使用上方代理组。端口范围 1024–65535。")
                        .font(.caption2).foregroundColor(ClaudeTheme.muted)
                }.padding(.vertical, 6)
            }
            if !vm.environmentChanges.isEmpty {
                GroupBox("本次修改预览") {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(Array(vm.environmentChanges.enumerated()), id: \.offset) { _, change in
                            Text("• " + change).font(.caption).fixedSize(horizontal: false, vertical: true)
                        }
                        if !vm.hasEnvironmentPlan {
                            Text("配置已变化或预览已执行；应用前请重新预览。")
                                .font(.caption2).foregroundColor(ClaudeTheme.warning)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 5)
                }
            }
            HStack {
                Button("读取当前配置") { vm.environmentCommand("status") }
                Spacer()
                Button("预览修改") { vm.environmentCommand("plan") }.disabled(!vm.environmentLoaded || vm.environmentSettings == nil)
                Button("应用配置") { vm.environmentCommand("apply") }
                    .buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
                    .disabled(!vm.hasEnvironmentPlan)
            }
            Text("应用或恢复可能短暂重启 Clash Verge，网络连接可能中断。配置变更会保留可恢复的备份。")
                .font(.caption2).foregroundColor(ClaudeTheme.muted)
            Button("恢复上一次网络配置") { vm.environmentCommand("restore") }
                .foregroundColor(ClaudeTheme.terracottaDeep)
        }.disabled(busy)
    }

    private var systemSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("地区目标") {
                VStack(alignment: .leading, spacing: 10) {
                    Text(vm.regionSummary).font(.callout.weight(.medium)).textSelection(.enabled)
                    Text("更换国家节点后，可按实际出口重新识别国家、时区与地区格式，并同步保护服务。")
                        .font(.caption).foregroundColor(ClaudeTheme.muted)
                    Button("按当前出口配置地区") { vm.configureSystem(detectRegion: true) }
                        .buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6)
            }
            GroupBox("系统与环境修复") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("应用当前地区目标、客户端语言与 IPv6 设置，修复 TUN、托管分流和保护服务，完成后重新检查。")
                        .font(.caption).foregroundColor(ClaudeTheme.muted)
                    Button("应用当前目标并修复环境") { vm.configureSystem(detectRegion: false) }
                    Text("操作会要求管理员验证，也可能重启代理。")
                        .font(.caption2).foregroundColor(ClaudeTheme.muted)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6)
            }
            Text("系统设置以实际出口为依据。字体、GPU、浏览器版本等设备特征保持设备自身状态。")
                .font(.caption2).foregroundColor(ClaudeTheme.muted)
        }.disabled(busy)
    }

    private var browserSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("连接 AdsPower") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("先在 AdsPower 启用 Local API。只配置你选择的已关闭环境；配置过程不会启动浏览器。")
                        .font(.caption).foregroundColor(ClaudeTheme.muted)
                    TextField("Local API 地址", text: $browserEndpoint).textFieldStyle(.roundedBorder)
                    SecureField("API Key（仅本次窗口使用）", text: $browserAPIKey).textFieldStyle(.roundedBorder)
                    HStack {
                        Button("读取浏览器环境") { requestBrowser("list") }
                        if vm.browserHasMore {
                            Button("读取更多") { requestBrowser("list", page: vm.browserPage + 1) }
                        }
                        Spacer()
                        Text("密钥不会保存到设置").font(.caption2).foregroundColor(ClaudeTheme.muted)
                    }
                    if !vm.browserProfiles.isEmpty {
                        Picker("浏览器环境", selection: $browserProfileID) {
                            Text("请选择环境").tag("")
                            ForEach(vm.browserProfiles) { profile in
                                Text(profile.name + " · " + profile.id).tag(profile.id)
                            }
                        }
                    }
                    TextField("浏览器环境 ID（也可手动填写）", text: $browserProfileID).textFieldStyle(.roundedBorder)
                    HStack {
                        Button("配置选中环境") { requestBrowser("configure") }
                            .buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
                        Button("恢复选中环境") { requestBrowser("restore") }
                    }.disabled(browserProfileID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }.padding(.vertical, 6)
            }.disabled(busy)
            GroupBox("手动配置参数") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("若当前 AdsPower 套餐未开放 API，可在浏览器环境设置中填写以下参数。先到网络页应用专用代理入口。")
                        .font(.caption).foregroundColor(ClaudeTheme.muted)
                    Text("代理类型：HTTP\n代理主机：127.0.0.1\n代理端口：\(vm.browserPort)\n用户名与密码：留空\nWebRTC：禁用")
                        .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    Button("复制配置参数") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString("HTTP 127.0.0.1:\(vm.browserPort)\n用户名与密码：留空\nWebRTC：禁用", forType: .string)
                    }
                    Text("若 API 未返回 WebRTC 原设置，将只配置代理，并提示你到 AdsPower 手动禁用。网络配置成功不代表 WebRTC 已完成。")
                        .font(.caption2).foregroundColor(ClaudeTheme.warning)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6)
            }
        }
    }

    private func requestBrowser(_ action: String, page: Int = 1) {
        vm.browserCommand(action, endpoint: browserEndpoint.trimmingCharacters(in: .whitespacesAndNewlines),
                          apiKey: browserAPIKey, profileID: browserProfileID.trimmingCharacters(in: .whitespacesAndNewlines), page: page)
    }

    private func messageBox(_ text: String, tone: ConfigurationTone) -> some View {
        let color: Color = tone == .failure ? ClaudeTheme.error : tone == .warning ? ClaudeTheme.warning : tone == .success ? ClaudeTheme.success : ClaudeTheme.muted
        return Group {
            if text.count <= 160 && !text.contains("\n") {
                Text(text).font(.caption).foregroundColor(color)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            } else {
                ScrollView {
                    Text(text).font(.caption).foregroundColor(color)
                        .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                }.frame(maxHeight: 100)
            }
        }
        .padding(10).background(color.opacity(0.07))
        .clipShape(RoundedRectangle(cornerRadius: 9))
    }
}

// MARK: - 设备指纹体检

struct FingerprintView: View {
    @Environment(\.dismiss) private var dismiss
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("设备指纹体检").font(.headline).foregroundColor(ClaudeTheme.ink)
                Spacer()
                Button("关闭") { dismiss() }.buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
            }
            Text("字体地区特征 + GPU/虚拟机特征；Canvas/WebGL 请用 Web 检测页")
                .font(.caption).foregroundColor(ClaudeTheme.muted)
            ScrollView {
                Text(text.isEmpty ? "正在体检…" : text)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundColor(ClaudeTheme.ink)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(20)
        .frame(width: 520, height: 420)
        .preferredColorScheme(.light)
        .background(ClaudeTheme.bgTop)
    }
}

// MARK: - 版本更新

struct UpdateSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var vm: VM
    @State private var copied = false

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("版本更新").font(.title3.bold()).foregroundColor(ClaudeTheme.ink)
                        Text("只检查，不写入任何文件").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(ClaudeTheme.muted)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("关闭")
                }

                ScrollView {
                    Text(vm.updateText.isEmpty ? "尚未检查" : vm.updateText)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundColor(ClaudeTheme.ink)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(12)
                .frame(height: 110)
                .background(ClaudeTheme.card)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(ClaudeTheme.cardBorder))

                HStack(spacing: 10) {
                    Button { vm.checkUpdate() } label: {
                        Label("重新检查", systemImage: "arrow.clockwise")
                            .font(.callout.bold())
                            .padding(.horizontal, 16).padding(.vertical, 7)
                            .background(LinearGradient(colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep], startPoint: .leading, endPoint: .trailing))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Button { copyUpdateCommand() } label: {
                        Label(copied ? "已复制" : "复制更新命令", systemImage: copied ? "checkmark" : "doc.on.doc")
                            .font(.callout.bold())
                            .padding(.horizontal, 16).padding(.vertical, 7)
                            .background(ClaudeTheme.iconWell)
                            .foregroundColor(ClaudeTheme.ink)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Spacer()
                }

                Text("复制后到终端执行即可更新；更新前会做 SHA256 校验，自检不通过会自动回滚。")
                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
            }
            .padding(20)
        }
        .frame(width: 520, height: 300)
        .preferredColorScheme(.light)
    }

    private func copyUpdateCommand() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        _ = pasteboard.setString("~/.local/bin/claude-switch.sh update", forType: .string)
        copied = true
    }
}

// MARK: - AI 交接提示词

struct AIPromptSheet: View {
    @Environment(\.dismiss) private var dismiss
    let text: String
    @State private var copied = false

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("AI 交接提示词").font(.title3.bold()).foregroundColor(ClaudeTheme.ink)
                        Text("复制后粘贴给本地 AI，让它先读 AGENTS.md 再动手").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(ClaudeTheme.muted)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("关闭")
                }

                ScrollView {
                    Text(text)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundColor(ClaudeTheme.ink)
                        .textSelection(.enabled)
                        .lineSpacing(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(12)
                .background(ClaudeTheme.card)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(ClaudeTheme.cardBorder))

                HStack {
                    Button { copyPrompt() } label: {
                        Label(copied ? "已复制" : "复制", systemImage: copied ? "checkmark" : "doc.on.doc")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 7)
                            .background(LinearGradient(colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep], startPoint: .leading, endPoint: .trailing))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Spacer()
                }
            }
            .padding(20)
        }
        .frame(width: 520, height: 480)
        .preferredColorScheme(.light)
    }

    private func copyPrompt() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        _ = pasteboard.setString(text, forType: .string)
        copied = true
    }
}

// MARK: - 公告页

struct NoticeView: View {
    @Environment(\.dismiss) private var dismiss

    let sections: [(String, String)] = [
        ("Full-Tunnel 全流量接管", "基于 TUN 虚拟网卡的内核级流量劫持，配合 Fake-IP DNS 解析机制，包括 UDP 在内的全部出站流量在网络栈底层被强制路由至加密隧道，应用层无法旁路。"),
        ("Environment Audit 环境一致性审计", "每次启动前执行 14 项环境指纹审计：Egress IP 归属地与 ASN 属性、时区与 Geo-location 一致性、Locale、IPv6 双栈旁路、DNS Leak、WebRTC/STUN 多节点交叉探测等。硬性安全项失败即 Block-on-Fail；仅显式配置的非关键项可跳过或降级，skip 不表示通过。"),
        ("Kill-Switch 断网急停", "常驻守护进程以 10 秒周期巡检隧道健康度，隧道中断即刻终止受保护进程，将裸连窗口压缩到秒级。进程级急停，非防火墙级保证，边界详见 SECURITY.md。"),
        ("Pinned Routing 域名置顶路由", "目标服务全家族域名及其生态依赖（Turnstile 人机验证、Telemetry 遥测、Payment 支付、Support 组件）置顶锁定代理路由，优先级高于任何第三方分流规则集，免疫规则库误判。"),
        ("Session Isolation 会话隔离快照", "多账号采用完整快照隔离：各账号持有独立的 Device Fingerprint、OAuth Credential 与本地状态，切换即物理交换数据卷，账号间零交叉污染。"),
        ("Drift Self-Healing 配置漂移自愈", "针对系统时区漂移、代理组件升级导致的配置重置等常见问题，内置 Watchdog 周期性校验并自动回滚至基线状态，全程留痕、可审计。"),
        ("Anti-Tracking 反追踪卫生", "验证链接（Magic Link）闭环于受保护环境内打开；邮件追踪像素（Tracking Pixel）经由邮箱服务商代理层屏蔽；全程固定单一住宅出口，不产生 Geo 跳变。"),
    ]

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(spacing: 0) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("安全防护体系公告").font(.title3.bold()).foregroundColor(ClaudeTheme.ink)
                        Text("Security Architecture Bulletin").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(ClaudeTheme.muted)
                    }
                    .buttonStyle(.plain)
                }
                .padding(20)

                ScrollView {
                    VStack(spacing: 10) {
                        Text("本产品面向对账号环境一致性有严格要求的专业用户，基于 Fail-Closed（故障即断）原则构建，针对目标平台的 Risk Control 体系提供全链路防护，确保本地真实出口在任何场景下零暴露。")
                            .font(.callout)
                            .foregroundColor(ClaudeTheme.muted)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.bottom, 6)

                        ForEach(Array(sections.enumerated()), id: \.offset) { idx, s in
                            VStack(alignment: .leading, spacing: 5) {
                                Text("\(idx + 1). \(s.0)").font(.callout.bold()).foregroundColor(ClaudeTheme.terracottaDeep)
                                Text(s.1).font(.caption).foregroundColor(ClaudeTheme.muted).lineSpacing(3)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .background(ClaudeTheme.card)
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                        }

                        Text("本防护体系基于对目标平台风控机制的持续逆向分析与实战经验迭代。")
                            .font(.caption2)
                            .foregroundColor(ClaudeTheme.muted)
                            .padding(.top, 8)
                    }
                    .padding(.horizontal, 20).padding(.bottom, 20)
                }
            }
        }
        .frame(width: 460, height: 620)
        .preferredColorScheme(.light)
    }
}

// MARK: - App

class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let vm: VM
    private(set) var mainWindow: NSWindow?

    override init() {
        vm = VM()
        super.init()
    }

    // 隔离测试可注入假核心脚本，不需要修改 HOME 或接触真实账号。
    init(vm: VM) {
        self.vm = vm
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        vm.onSwitchCompleted = { [weak self] in self?.mainWindow?.orderOut(nil) }
        showMainWindow()
        vm.start()
    }

    func showMainWindow() {
        vm.prepareToShow()
        if mainWindow == nil {
            let style: NSWindow.StyleMask = [.titled, .closable, .miniaturizable]
            let titleHeight = NSWindow.frameRect(forContentRect: .zero, styleMask: style).height
            let height = min(CGFloat(900), (NSScreen.main?.visibleFrame.height ?? 900) - titleHeight)
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 440, height: height),
                styleMask: style,
                backing: .buffered, defer: false
            )
            window.title = "Claude切换器"
            window.titlebarAppearsTransparent = true
            window.titleVisibility = .hidden
            window.isMovableByWindowBackground = true
            window.isReleasedWhenClosed = false
            window.standardWindowButton(.zoomButton)?.isEnabled = false
            window.contentView = NSHostingView(rootView: ContentView(vm: vm, height: height))
            window.delegate = self
            window.center()
            mainWindow = window
        }
        if mainWindow?.isMiniaturized == true { mainWindow?.deminiaturize(nil) }
        mainWindow?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        vm.canQuit ? .terminateNow : .terminateCancel
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return false
    }
}

struct MenuBarContent: View {
    @ObservedObject var vm: VM
    let showWindow: () -> Void

    var body: some View {
        Text("Claude 切换器")
        Text("当前账号：\(vm.current)")
        Text("最近检查：\(vm.lastCheckSummary)")
        if vm.phase == .fixing { Text("正在修复…") }
        if vm.phase == .switching { Text("正在切换账号…") }
        Divider()
        Button("打开主窗口", action: showWindow)
            .keyboardShortcut("o")
        Button("重新检查") {
            showWindow()
            vm.refresh()
        }
        .disabled(vm.isBusy)
        Button("检查更新") {
            showWindow()
            vm.checkUpdate()
            vm.showUpdateSheet = true
        }
        Button("环境配置") {
            showWindow()
            vm.showEnvironmentSheet = true
        }
        .disabled(vm.isBusy || vm.settingsBusy)
        Button("检查设置") {
            showWindow()
            vm.showSettingsSheet = true
        }
        .disabled(vm.isBusy || vm.settingsBusy)
        Button("导出诊断日志") { vm.exportDiagnostics() }
            .disabled(vm.isBusy || vm.settingsBusy)
        Divider()
        Button("退出 Claude 切换器") { NSApplication.shared.terminate(nil) }
            .keyboardShortcut("q")
            .disabled(!vm.canQuit)
            .onAppear { vm.refreshAccountStatus() }
    }
}

// 生产应用与 UI 测试共用完全相同的菜单和窗口管理。
struct SwitcherMenu: Scene {
    @ObservedObject var vm: VM
    let controller: AppDelegate

    private static let statusImage: NSImage = {
        let image = NSImage(systemSymbolName: "shield.lefthalf.filled", accessibilityDescription: "Claude 切换器")!
        image.isTemplate = true
        return image
    }()

    var body: some Scene {
        MenuBarExtra {
            MenuBarContent(vm: vm, showWindow: controller.showMainWindow)
        } label: {
            Image(nsImage: Self.statusImage)
                .accessibilityLabel("Claude 切换器")
        }
        .menuBarExtraStyle(.menu)
        .commands {
            CommandGroup(replacing: .appTermination) {
                Button("退出 Claude 切换器") { NSApplication.shared.terminate(nil) }
                    .keyboardShortcut("q")
                    .disabled(!vm.canQuit)
            }
        }
    }
}

#if !CLAUDE_SWITCHER_TESTS
@main
struct SwitcherApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        SwitcherMenu(vm: appDelegate.vm, controller: appDelegate)
    }
}
#endif

import AppKit
import ScreenCaptureKit
import CC8WindowCore

// This helper receives one bounded request through an inherited pipe. It writes
// one result through another pipe; neither pixels nor window names go to disk.
struct PickerRequest: Decodable {
    let request_id: String
    let duration: Double
    let question: String
    let context: String
}

func readRequest() throws -> PickerRequest {
    var data = Data()
    while data.count <= 8192 {
        let byte = FileHandle.standardInput.readData(ofLength: 1)
        guard !byte.isEmpty else { throw WindowInputError.invalidRequest }
        if byte == Data([10]) {
            let request = try JSONDecoder().decode(PickerRequest.self, from: data)
            guard !request.question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  request.question.count <= 1000, request.context.count <= 120 else {
                throw WindowInputError.invalidRequest
            }
            return request
        }
        data.append(byte)
    }
    throw WindowInputError.invalidRequest
}

@available(macOS 15.2, *)
final class PickerObserver: NSObject, SCContentSharingPickerObserver {
    weak var owner: PickerApplication?
    let ticket: Int
    init(_ owner: PickerApplication, ticket: Int) { self.owner = owner; self.ticket = ticket }
    func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }; self.owner?.pickerCancelled(self)
        }
    }
    func contentSharingPicker(_ picker: SCContentSharingPicker,
                              didUpdateWith filter: SCContentFilter, for stream: SCStream?) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }; self.owner?.selected(filter, observer: self)
        }
    }
    func contentSharingPickerStartDidFailWithError(_ error: Error) {
        let outcome = captureFailure(error)
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }; self.owner?.pickerFailed(self, outcome: outcome)
        }
    }
}

@available(macOS 15.2, *)
final class PickerApplication: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private struct SelectedSource {
        let ticket: Int
        let identity: WindowIdentity
        let application: NSRunningApplication
    }

    let request: PickerRequest
    let lease: SelectionLease
    private let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 640),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
    private let imageView = NSImageView()
    private let questionField = NSTextField()
    private let scope = NSTextField(wrappingLabelWithString: "尚未选择窗口。只发送预览中这一次的画面。")
    private let choose = NSButton(title: "选择／重新选择窗口", target: nil, action: nil)
    private let send = NSButton(title: "发送这一张给 Dot", target: nil, action: nil)
    private var observer: PickerObserver?
    private var timer: Timer?
    private var selectedSource: SelectedSource?
    private var terminal = false

    init(request: PickerRequest) throws {
        self.request = request
        lease = try SelectionLease(requestID: request.request_id, duration: request.duration,
                                   ownProcessID: ProcessInfo.processInfo.processIdentifier)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        window.title = "大黄蜂 · 选择 Mac 窗口"
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()
        let heading = NSTextField(wrappingLabelWithString:
            "单个窗口 · 单次画面 · \(request.context)\n问题可在发送前修改：")
        heading.font = .systemFont(ofSize: 16, weight: .medium)
        questionField.stringValue = request.question
        questionField.font = .systemFont(ofSize: 15)
        questionField.isEditable = true
        questionField.isSelectable = true
        questionField.isBezeled = true
        questionField.drawsBackground = true
        questionField.cell?.wraps = true
        questionField.cell?.isScrollable = false
        scope.font = .systemFont(ofSize: 14)
        imageView.imageScaling = .scaleProportionallyUpOrDown
        imageView.setContentHuggingPriority(.defaultLow, for: .vertical)
        choose.target = self; choose.action = #selector(chooseWindow)
        send.target = self; send.action = #selector(sendPreview); send.isEnabled = false
        let cancel = NSButton(title: "取消", target: self, action: #selector(cancelRequest))
        let buttons = NSStackView(views: [choose, send, cancel])
        buttons.orientation = .horizontal; buttons.spacing = 14
        let stack = NSStackView(views: [heading, questionField, scope, imageView, buttons])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView?.addSubview(stack)
        if let content = window.contentView {
            NSLayoutConstraint.activate([
                stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
                stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
                stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
                stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -24),
                questionField.widthAnchor.constraint(equalTo: stack.widthAnchor),
                questionField.heightAnchor.constraint(equalToConstant: 60),
                imageView.widthAnchor.constraint(equalTo: stack.widthAnchor),
                imageView.heightAnchor.constraint(greaterThanOrEqualToConstant: 300)
            ])
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        let timer = Timer(timeInterval: 0.2, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if self.lease.phase == .expired {
                self.finish(status: "expired")
            } else if let source = self.selectedSource, !self.stillExists(source) {
                self.revokeSelection(source.ticket)
            }
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
        // EOF from the owning parent revokes the preview, even while the system
        // picker is open. A helper can never outlive its initiating connection.
        DispatchQueue.global(qos: .utility).async { [weak self] in
            _ = FileHandle.standardInput.readData(ofLength: 1)
            DispatchQueue.main.async { self?.finish(status: "cancelled") }
        }
    }

    @objc private func chooseWindow() {
        guard !terminal, NSApp.isActive, let ticket = lease.beginSelection() else { return }
        clearSelection()
        choose.isEnabled = false
        scope.stringValue = "请在系统面板明确选择一个窗口；最多三次选择，90 秒内完成。"
        let picker = SCContentSharingPicker.shared
        var config = SCContentSharingPickerConfiguration()
        config.allowedPickerModes = [.singleWindow]
        config.excludedBundleIDs = [Bundle.main.bundleIdentifier].compactMap { $0 }
        config.allowsChangingSelectedContent = false
        picker.defaultConfiguration = config
        let current = PickerObserver(self, ticket: ticket)
        observer = current; picker.add(current); picker.isActive = true
        picker.present(using: .window)
    }

    private func detach(_ current: PickerObserver) -> Bool {
        guard observer === current else { return false }
        SCContentSharingPicker.shared.remove(current)
        observer = nil
        return true
    }

    func pickerCancelled(_ current: PickerObserver) {
        if detach(current), acceptsCapture(current.ticket) { finish(status: "cancelled") }
    }

    func pickerFailed(_ current: PickerObserver, outcome: String) {
        if detach(current), acceptsCapture(current.ticket) { finish(status: outcome) }
    }

    private func clearSelection() {
        selectedSource = nil
        imageView.image = nil
        send.isEnabled = false
    }

    private func revokeSelection(_ ticket: Int) {
        guard lease.revoke(ticket: ticket) else { return }
        clearSelection()
        SCContentSharingPicker.shared.isActive = false
        choose.isEnabled = lease.canSelect
        scope.stringValue = choose.isEnabled
            ? "原窗口已关闭或无法核对。请重新选择，或取消。"
            : "原窗口已关闭或无法核对。已用完三次选择，请关闭后重新发起。"
    }

    // Query exactly the selected ID and retain the original running application
    // so a restarted process cannot validate the earlier selection.
    private func stillExists(_ source: SelectedSource) -> Bool {
        let identity = source.identity
        let application = source.application
        guard !application.isTerminated,
              application.processIdentifier == identity.processID,
              let rows = CGWindowListCreateDescriptionFromArray([NSNumber(value: identity.id)] as CFArray)
                as? [[String: Any]], rows.count == 1,
              let row = rows.first,
              (row[kCGWindowNumber as String] as? NSNumber)?.uint32Value == identity.id,
              (row[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == identity.processID
        else { return false }
        return !application.isTerminated
    }

    private func acceptsCapture(_ ticket: Int) -> Bool {
        guard !terminal else { return false }
        if lease.isSelecting(ticket: ticket) { return true }
        if lease.phase == .expired { finish(status: "expired") }
        return false
    }

    func selected(_ filter: SCContentFilter, observer current: PickerObserver) {
        guard detach(current), acceptsCapture(current.ticket) else { return }
        guard filter.style == .window, filter.includedWindows.count == 1,
              let chosen = filter.includedWindows.first,
              let app = chosen.owningApplication else { finish(status: "selection_invalid"); return }
        let identity = WindowIdentity(id: chosen.windowID, processID: app.processID,
                                      application: app.applicationName, title: chosen.title ?? "未命名窗口")
        guard identity.processID != ProcessInfo.processInfo.processIdentifier,
              let application = NSRunningApplication(processIdentifier: identity.processID) else {
            revokeSelection(current.ticket)
            return
        }
        let source = SelectedSource(ticket: current.ticket, identity: identity, application: application)
        guard stillExists(source) else {
            revokeSelection(current.ticket)
            return
        }
        let dimensions = try? WindowImage.size(width: filter.contentRect.width,
                                               height: filter.contentRect.height)
        guard acceptsCapture(source.ticket) else { return }
        guard let dimensions = dimensions else {
            finish(status: "selection_invalid"); return
        }
        selectedSource = source
        scope.stringValue = "正在取图：\(identity.application) · \(identity.title) · 仅选定窗口"
        let config = SCStreamConfiguration()
        config.width = dimensions.0; config.height = dimensions.1
        config.capturesAudio = false; config.captureMicrophone = false
        config.showsCursor = false; config.ignoreShadowsSingleWindow = true
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { [weak self] image, error in
            let outcome = captureFailure(error)
            // Encoding only the selected image runs off the UI thread. Both the
            // attempt ticket and original window identity are checked afterwards.
            DispatchQueue.global(qos: .userInitiated).async {
                guard let lease = self?.lease, lease.isSelecting(ticket: source.ticket) else { return }
                let encoded = error == nil ? image.flatMap { try? WindowImage.encode($0) } : nil
                DispatchQueue.main.async {
                    guard let self = self, self.acceptsCapture(source.ticket) else { return }
                    if outcome == "permission_denied" {
                        self.finish(status: outcome)
                        return
                    }
                    guard self.stillExists(source) else {
                        self.revokeSelection(source.ticket)
                        return
                    }
                    guard self.acceptsCapture(source.ticket) else { return }
                    guard let (data, width, height) = encoded else {
                        self.finish(status: "capture_failed"); return
                    }
                    let frame = WindowFrame(identity: identity, jpeg: data, width: width, height: height)
                    guard self.lease.accept(frame, ticket: source.ticket, isSingleWindow: true) else { return }
                    SCContentSharingPicker.shared.isActive = false
                    self.imageView.image = NSImage(data: data)
                    self.scope.stringValue = "\(identity.application) · \(identity.title)\n仅此窗口的冻结画面 · \(width) × \(height) · 点击发送才交给 Dot"
                    NSApp.activate(ignoringOtherApps: true)
                    self.window.makeKeyAndOrderFront(nil)
                    self.send.isEnabled = true
                    self.choose.isEnabled = self.lease.canSelect
                }
            }
        }
    }

    @objc private func sendPreview() {
        let question = questionField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty, question.unicodeScalars.count <= 1000 else {
            scope.stringValue = "请填写 1–1000 字的问题，再确认发送当前预览。"
            return
        }
        guard NSApp.isActive, let source = selectedSource, lease.preview != nil else { return }
        guard stillExists(source) else {
            revokeSelection(source.ticket)
            return
        }
        guard let frame = lease.take(ticket: source.ticket) else { return }
        finish(status: "completed", frame: frame, question: question)
    }

    @objc private func cancelRequest() { finish(status: "cancelled") }
    func windowShouldClose(_ sender: NSWindow) -> Bool { finish(status: "cancelled"); return false }
    func applicationDidResignActive(_ notification: Notification) {
        if lease.phase == .preview { finish(status: "cancelled") }
    }

    private func finish(status: String, frame: WindowFrame? = nil, question: String? = nil) {
        guard !terminal else { return }; terminal = true
        lease.cancel(); timer?.invalidate(); timer = nil
        if let current = observer { _ = detach(current) }
        SCContentSharingPicker.shared.isActive = false
        clearSelection()
        var result: [String: Any] = ["status": status, "request_id": request.request_id]
        if let frame = frame, let question = question {
            result.merge(["source_type": "mac_window", "window_id": frame.identity.id,
                          "question": question,
                          "process_id": frame.identity.processID, "application": frame.identity.application,
                          "title": frame.identity.title, "width": frame.width, "height": frame.height,
                          "image_base64": frame.jpeg.base64EncodedString()]) { _, new in new }
        }
        if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) {
            FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data([10]))
        }
        window.orderOut(nil); NSApp.terminate(nil)
    }
}

if #available(macOS 15.2, *) {
    do {
        let request = try readRequest()
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = try PickerApplication(request: request)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    } catch { exit(2) }
} else {
    // No older-system fallback may widen the scope to the full display.
    exit(3)
}

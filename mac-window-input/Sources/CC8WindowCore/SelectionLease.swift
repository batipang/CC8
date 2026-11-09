import Foundation

public struct WindowIdentity: Equatable {
    public let id: UInt32
    public let processID: Int32
    public let application: String
    public let title: String

    public init(id: UInt32, processID: Int32, application: String, title: String) {
        self.id = id
        self.processID = processID
        self.application = String(application.prefix(100))
        self.title = String(title.prefix(240))
    }
}

public struct WindowFrame {
    public let identity: WindowIdentity
    public let jpeg: Data
    public let width: Int
    public let height: Int

    public init(identity: WindowIdentity, jpeg: Data, width: Int, height: Int) {
        self.identity = identity
        self.jpeg = jpeg
        self.width = width
        self.height = height
    }
}

/// One request owns one immutable preview; choosing again does not extend its deadline.
/// The UI owns the expiry timer and calls expire() independently of user interaction.
public final class SelectionLease {
    public enum Phase: Equatable { case waiting, selecting, preview, finished, cancelled, expired }
    public let requestID: String
    public let deadline: TimeInterval
    private let ownProcessID: Int32
    private let clock: () -> TimeInterval
    private let lock = NSLock()
    private var currentPhase: Phase = .waiting
    private var selections = 0
    private var currentTicket = 0
    private var frame: WindowFrame?

    public init(requestID: String, duration: TimeInterval, ownProcessID: Int32,
                clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) throws {
        let now = clock()
        guard requestID.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil,
              duration.isFinite, duration >= 5, duration <= 90, now.isFinite, now >= 0,
              (now + duration).isFinite, ownProcessID > 0 else { throw WindowInputError.invalidRequest }
        self.requestID = requestID
        deadline = now + duration
        self.ownProcessID = ownProcessID
        self.clock = clock
    }

    private func expiredLocked() -> Bool {
        if currentPhase == .finished || currentPhase == .cancelled || currentPhase == .expired { return true }
        let now = clock()
        if !now.isFinite || now < 0 || now >= deadline {
            frame = nil
            currentPhase = .expired
            currentTicket += 1
            return true
        }
        return false
    }

    public var phase: Phase {
        lock.lock(); defer { lock.unlock() }
        _ = expiredLocked()
        return currentPhase
    }

    public var preview: WindowFrame? {
        lock.lock(); defer { lock.unlock() }
        return expiredLocked() ? nil : frame
    }

    public func beginSelection() -> Int? {
        lock.lock(); defer { lock.unlock() }
        guard !expiredLocked(), selections < 3,
              currentPhase == .waiting || currentPhase == .preview else { return nil }
        frame = nil
        selections += 1
        currentTicket += 1
        currentPhase = .selecting
        return currentTicket
    }

    public func accept(_ candidate: WindowFrame, ticket: Int, isSingleWindow: Bool) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !expiredLocked(), currentPhase == .selecting, ticket == currentTicket,
              isSingleWindow, candidate.identity.id > 0, candidate.identity.processID > 0,
              candidate.identity.processID != ownProcessID,
              candidate.width > 0, candidate.height > 0,
              candidate.width <= 1280, candidate.height <= 1280,
              candidate.width * candidate.height <= 1280 * 960,
              !candidate.jpeg.isEmpty, candidate.jpeg.count <= 768 * 1024,
              candidate.jpeg.starts(with: [0xff, 0xd8]), candidate.jpeg.suffix(2) == Data([0xff,0xd9])
        else { return false }
        frame = candidate
        currentPhase = .preview
        return true
    }

    public func take(ticket: Int) -> WindowFrame? {
        lock.lock(); defer { lock.unlock() }
        guard !expiredLocked(), currentPhase == .preview, ticket == currentTicket, let value = frame else { return nil }
        frame = nil
        currentPhase = .finished
        currentTicket += 1
        return value
    }

    public func cancel() {
        lock.lock(); defer { lock.unlock() }
        guard currentPhase != .finished else { return }
        frame = nil
        currentTicket += 1
        currentPhase = .cancelled
    }

    public func expire() {
        lock.lock(); defer { lock.unlock() }
        _ = expiredLocked()
    }
}

public enum WindowInputError: Error { case invalidRequest, invalidImage, imageTooLarge }

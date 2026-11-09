import Foundation
import CoreGraphics
import ImageIO
import CC8WindowCore

private final class SelectionChecks {
    var failures: [String] = []
    func expectTrue(_ value: @autoclosure () -> Bool) { if !value() { failures.append("Expected true") } }
    func expectFalse(_ value: @autoclosure () -> Bool) { expectTrue(!value()) }
    func expectNil<T>(_ value: T?) { expectTrue(value == nil) }
    func expectNotNil<T>(_ value: T?) { expectTrue(value != nil) }
    func expectEqual<T: Equatable>(_ a: T, _ b: T) { expectTrue(a == b) }
    func expectLess<T: Comparable>(_ a: T, _ b: T) { expectTrue(a < b) }
    func expectGreater<T: Comparable>(_ a: T, _ b: T) { expectTrue(a > b) }
    func expectAtMost<T: Comparable>(_ a: T, _ b: T) { expectTrue(a <= b) }
    func require<T>(_ value: T?) throws -> T {
        guard let value = value else { throw CheckFailure(message: "Required value missing") }; return value
    }
    func expectError<T>(_ expression: @autoclosure () throws -> T) {
        do { _ = try expression(); failures.append("Expected error") } catch {}
    }
    private var now = 100.0
    private func lease() throws -> SelectionLease {
        try SelectionLease(requestID:String(repeating:"a",count:32),duration:90,
                           ownProcessID:42,clock:{ self.now })
    }
    private func frame(pid: Int32 = 99) -> WindowFrame {
        WindowFrame(identity:WindowIdentity(id:7,processID:pid,application:"Synthetic",title:"Fixture"),
                    jpeg:Data([0xff,0xd8,0xff,0xd9]),width:640,height:480)
    }
    func testOnlySelectedWindowAndCurrentTicketCanProduceOneSend() throws {
        let value=try lease();let ticket=try require(value.beginSelection())
        expectNil(value.beginSelection())
        expectFalse(value.accept(frame(),ticket:ticket,isSingleWindow:false))
        expectFalse(value.accept(frame(pid:42),ticket:ticket,isSingleWindow:true))
        expectTrue(value.accept(frame(),ticket:ticket,isSingleWindow:true))
        expectNil(value.take(ticket:ticket+1));expectNotNil(value.preview)
        expectEqual(value.take(ticket:ticket)?.identity.id,7)
        expectNil(value.take(ticket:ticket));expectNil(value.preview)
        expectNil(value.beginSelection());expectEqual(value.phase,.finished)
    }
    func testRetakeRevokesLateCallbacksAndNeverExtendsDeadline() throws {
        let value=try lease();let first=try require(value.beginSelection())
        expectTrue(value.accept(frame(),ticket:first,isSingleWindow:true))
        now=130;let second=try require(value.beginSelection());expectNil(value.preview)
        expectFalse(value.accept(frame(),ticket:first,isSingleWindow:true))
        expectTrue(value.accept(frame(),ticket:second,isSingleWindow:true))
        expectEqual(value.deadline,190)
        now=190;value.expire();expectEqual(value.phase,.expired)
        expectNil(value.preview);expectNil(value.take(ticket:second))
    }
    func testThreeSelectionsAndCancellationAreBounded() throws {
        let value=try lease()
        for _ in 0..<3 {
            let ticket=try require(value.beginSelection())
            expectTrue(value.accept(frame(),ticket:ticket,isSingleWindow:true))
        }
        expectNil(value.beginSelection());value.cancel()
        expectNil(value.preview);expectNil(value.beginSelection())
        expectEqual(value.phase,.cancelled)
    }
    func testInvalidDeadlineAndIdentifierNeverCreateARequest() throws {
        for duration in [0.0,4.0,91.0,Double.infinity,Double.nan] {
            expectError(try SelectionLease(requestID:String(repeating:"a",count:32),duration:duration,ownProcessID:42))
        }
        expectError(try SelectionLease(requestID:"../not-an-id",duration:90,ownProcessID:42))
        expectError(try SelectionLease(requestID:String(repeating:"a",count:32),duration:90,ownProcessID:42,clock:{ .nan }))
    }
    func testSyntheticPixelsUseActualJPEGEncoderAndBoundedDimensions() throws {
        let color=try require(CGColorSpace(name:CGColorSpace.sRGB))
        let context=try require(CGContext(data:nil,width:2048,height:1024,bitsPerComponent:8,
                              bytesPerRow:2048*4,space:color,bitmapInfo:CGImageAlphaInfo.premultipliedLast.rawValue))
        context.setFillColor(CGColor(red:0,green:1,blue:0,alpha:1))
        context.fill(CGRect(x:0,y:0,width:2048,height:1024))
        let image=try require(context.makeImage())
        let (data,width,height)=try WindowImage.encode(image)
        expectEqual(width,1280);expectEqual(height,640);expectAtMost(data.count,768*1024)
        let decoded=try require(CGImageSourceCreateWithData(data as CFData,nil))
        let result=try require(CGImageSourceCreateImageAtIndex(decoded,0,nil))
        expectEqual(result.width,width);expectEqual(result.height,height)
        let pixel=try require(CGContext(data:nil,width:1,height:1,bitsPerComponent:8,
            bytesPerRow:4,space:color,bitmapInfo:CGImageAlphaInfo.premultipliedLast.rawValue))
        pixel.draw(result,in:CGRect(x:0,y:0,width:1,height:1))
        let bytes=try require(pixel.data).assumingMemoryBound(to:UInt8.self)
        expectLess(bytes[0],30);expectGreater(bytes[1],200);expectLess(bytes[2],30)
    }
    func testGeometryRejectsNonfiniteAndOversizedValuesBeforeIntegerConversion() throws {
        for pair in [(Double.nan,10.0),(10.0,Double.infinity),(0.0,10.0),(100001.0,10.0)] {
            expectError(try WindowImage.size(width:pair.0,height:pair.1))
        }
        let (width,height)=try WindowImage.size(width:100000,height:100000)
        expectAtMost(max(width,height),1280)
        expectAtMost(width*height,1280*960)
    }
    func testCancellationDuringEncodingCannotAcceptOrSendOldPixels() throws {
        let value=try lease();let ticket=try require(value.beginSelection())
        value.cancel()
        expectFalse(value.accept(frame(),ticket:ticket,isSingleWindow:true))
        expectNil(value.preview);expectNil(value.take(ticket:ticket))
    }

    func testConcurrentSendConsumesThePreviewExactlyOnce() throws {
        let value = try lease()
        let ticket = try require(value.beginSelection())
        expectTrue(value.accept(frame(), ticket: ticket, isSingleWindow: true))
        let lock = NSLock()
        var sent = 0
        DispatchQueue.concurrentPerform(iterations: 32) { _ in
            if value.take(ticket: ticket) != nil {
                lock.lock(); sent += 1; lock.unlock()
            }
        }
        expectEqual(sent, 1)
        expectNil(value.preview)
    }
}

private struct CheckFailure: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

/// Shared assertions for XCTest CI and CLT-only development; no screen or capture APIs.
public enum WindowCoreChecks {
    public static func runAll(report: (String) -> Void = { _ in }) throws {
        do { let checks = SelectionChecks(); try checks.testConcurrentSendConsumesThePreviewExactlyOnce(); if !checks.failures.isEmpty { throw CheckFailure(message: "concurrent send: " + checks.failures.joined(separator: ", ")) }; report("PASS testConcurrentSendConsumesThePreviewExactlyOnce") }
        do { let checks = SelectionChecks(); try checks.testOnlySelectedWindowAndCurrentTicketCanProduceOneSend(); if !checks.failures.isEmpty { throw CheckFailure(message: "testOnlySelectedWindowAndCurrentTicketCanProduceOneSend: " + checks.failures.joined(separator: ", ")) }; report("PASS testOnlySelectedWindowAndCurrentTicketCanProduceOneSend") }
        do { let checks = SelectionChecks(); try checks.testRetakeRevokesLateCallbacksAndNeverExtendsDeadline(); if !checks.failures.isEmpty { throw CheckFailure(message: "testRetakeRevokesLateCallbacksAndNeverExtendsDeadline: " + checks.failures.joined(separator: ", ")) }; report("PASS testRetakeRevokesLateCallbacksAndNeverExtendsDeadline") }
        do { let checks = SelectionChecks(); try checks.testThreeSelectionsAndCancellationAreBounded(); if !checks.failures.isEmpty { throw CheckFailure(message: "testThreeSelectionsAndCancellationAreBounded: " + checks.failures.joined(separator: ", ")) }; report("PASS testThreeSelectionsAndCancellationAreBounded") }
        do { let checks = SelectionChecks(); try checks.testInvalidDeadlineAndIdentifierNeverCreateARequest(); if !checks.failures.isEmpty { throw CheckFailure(message: "testInvalidDeadlineAndIdentifierNeverCreateARequest: " + checks.failures.joined(separator: ", ")) }; report("PASS testInvalidDeadlineAndIdentifierNeverCreateARequest") }
        do { let checks = SelectionChecks(); try checks.testSyntheticPixelsUseActualJPEGEncoderAndBoundedDimensions(); if !checks.failures.isEmpty { throw CheckFailure(message: "testSyntheticPixelsUseActualJPEGEncoderAndBoundedDimensions: " + checks.failures.joined(separator: ", ")) }; report("PASS testSyntheticPixelsUseActualJPEGEncoderAndBoundedDimensions") }
        do { let checks = SelectionChecks(); try checks.testGeometryRejectsNonfiniteAndOversizedValuesBeforeIntegerConversion(); if !checks.failures.isEmpty { throw CheckFailure(message: "testGeometryRejectsNonfiniteAndOversizedValuesBeforeIntegerConversion: " + checks.failures.joined(separator: ", ")) }; report("PASS testGeometryRejectsNonfiniteAndOversizedValuesBeforeIntegerConversion") }
        do { let checks = SelectionChecks(); try checks.testCancellationDuringEncodingCannotAcceptOrSendOldPixels(); if !checks.failures.isEmpty { throw CheckFailure(message: "testCancellationDuringEncodingCannotAcceptOrSendOldPixels: " + checks.failures.joined(separator: ", ")) }; report("PASS testCancellationDuringEncodingCannotAcceptOrSendOldPixels") }
    }
}

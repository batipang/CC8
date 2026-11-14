import Foundation
import ScreenCaptureKit

/// Only a documented ScreenCaptureKit denial is evidence of a permissions failure.
public func captureFailure(_ error: Error?) -> String {
    if let cause = error as NSError?, cause.domain == SCStreamError.errorDomain,
       cause.code == SCStreamError.Code.userDeclined.rawValue {
        return "permission_denied"
    }
    return "capture_failed"
}

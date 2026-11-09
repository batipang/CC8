import Foundation
import CC8WindowChecks

do {
    try WindowCoreChecks.runAll(report: { print($0) })
} catch {
    FileHandle.standardError.write(Data("Window core checks failed: \(error)\n".utf8))
    exit(1)
}

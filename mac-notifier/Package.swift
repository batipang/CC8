// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CC8Notifier",
    platforms: [.macOS(.v12)],
    products: [.executable(name: "CC8Notifier", targets: ["CC8Notifier"])],
    targets: [.executableTarget(name: "CC8Notifier")]
)

// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CC8WindowInput",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "CC8WindowCore", targets: ["CC8WindowCore"]),
        .executable(name: "cc8-window-check", targets: ["CC8WindowCoreCheck"]),
        .executable(name: "cc8-window-picker", targets: ["CC8WindowPicker"])
    ],
    targets: [
        .target(name: "CC8WindowCore"),
        .target(name: "CC8WindowChecks", dependencies: ["CC8WindowCore"]),
        .executableTarget(name: "CC8WindowCoreCheck", dependencies: ["CC8WindowChecks"]),
        .executableTarget(name: "CC8WindowPicker", dependencies: ["CC8WindowCore"]),
        .testTarget(name: "CC8WindowCoreTests", dependencies: ["CC8WindowChecks"])
    ]
)

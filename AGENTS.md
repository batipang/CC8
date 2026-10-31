# CC8 development conventions

Keep APK installation in normal Android mode, through the verified engineering shell / stock PackageInstaller path. Require AILABS_S6, SDK 27, UID 2000, one unambiguous USB target, hash checks and actual installation completion. Do not introduce Root, flashing, partition writes, BootROM tools, network ADB or OEM service termination.

Keep private credentials, signing keys, pairing material, device identifiers, raw reports and user recordings out of Git. Build outputs, local SDK/JDK/config and model weights belong in ignored local directories. Preserve signature compatibility when updating existing apps; do not silently uninstall or clear data.

Android code uses Java and platform widgets, minSdk 21, with newer API guards. Microphone and camera capture require explicit foreground user action and runtime permission. Stop resources on background or loss of every authenticated transport. Reconnect must never automatically enable capture. Never open a camera or microphone solely to verify an unrelated code change.

The assistant is 大黄蜂 / Bumblebee. Preserve its approved head-only avatar and separate particle waveform. USB has priority over paired TLS Wi-Fi. Reject stale audio, captions and results on cancellation / handover. Dot inherits owner-configured tools and permissions without automatic approvals. MiniMax is the default bounded utterance ASR and TTS; local AEC/VAD and interruption stay on the Mac.

Run meaningful host tests and Android compile/lint for changes to those components. Distinguish offline test coverage from physical-device validation; do not claim arbitrary firmware compatibility, proven AEC or complete full-duplex performance from API availability alone.

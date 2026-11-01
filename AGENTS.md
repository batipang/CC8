# CC8 development conventions

Keep APK installation in normal Android mode, through the verified engineering shell / stock PackageInstaller path. Require AILABS_S6, SDK 27, UID 2000, one unambiguous USB target, hash checks and actual installation completion. Do not introduce Root, flashing, partition writes, BootROM tools, network ADB or OEM service termination.

Keep private credentials, signing keys, pairing material, device identifiers, raw reports and user recordings out of Git. Build outputs, local SDK/JDK/config and model weights belong in ignored local directories. Preserve signature compatibility when updating existing apps; do not silently uninstall or clear data.

Android code uses Java and platform widgets, minSdk 21, with newer API guards. Camera capture requires explicit foreground user action and runtime permission. The user explicitly requested automatic voice on opening the assistant (2026-11-01); a visible MainActivity opening may arm one bounded microphone start after connection and permission. Stop resources on background or loss of every authenticated transport. Reconnect must never automatically enable capture. Never open a camera or microphone solely to verify an unrelated code change.

The assistant is 大黄蜂 / Bumblebee. Preserve its approved head-only avatar and separate particle waveform. USB has priority over paired TLS Wi-Fi. Reject stale audio, captions and results on cancellation / handover. Dot inherits owner-configured tools and permissions without automatic approvals. MiniMax is the default bounded utterance ASR and TTS; local AEC/VAD and interruption stay on the Mac.

Run meaningful host tests and Android compile/lint for changes to those components. Distinguish offline test coverage from physical-device validation; do not claim arbitrary firmware compatibility, proven AEC or complete full-duplex performance from API availability alone.

DesktopEntryService is a UI-only foreground service authorized for reopening the app from the OEM desktop. It may keep a silent notification and an explicitly permitted overlay after Home, but must never hold microphone/camera/audio/network resources; the entry may open MainActivity, whose user-requested opening flow can start voice. Hide its overlay inside assistant/camera pages. Close TLS sockets off the UI thread without relaxing StrictMode. Preserve the original signing certificate when deploying updates.

Automatic voice is one-shot per MainActivity creation or explicit new open intent, with a 30-second connection wait. Manual Stop cancels the request; transport reconnection and ordinary onResume alone cannot rearm it. Home still stops capture. For text-only announcements, maintenance or unrelated QA, use launch.py --no-auto-voice / --no-open or the auto_start_voice=false intent extra. Do not open an automatically recording UI merely to inspect status.

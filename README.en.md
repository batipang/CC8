# CC8 APK installation and Bumblebee assistant

[中文](README.md) · [Documentation index](docs/README.md) · [Compatibility](docs/COMPATIBILITY.md)

This repository contains the normal-Android APK installer, offline diagnostic app and the personal Bumblebee assistant. The verified hardware is **one TG_S6B01 / AILABS_S6, SDK 27, firmware 6.1.8-RS-20251125.1919**. Other models and firmware remain unverified.

The verified installation route uses the engineering command channel as shell UID 2000 and the stock PackageInstaller API. It does not require root, flashing, bootloader changes or network ADB. The protected staging path is blocked on this device; the optional explicit shared-staging route retains a helper-replacement race. Read the [installation procedure](docs/APK_INSTALL.md) and [troubleshooting](docs/INSTALL_TROUBLESHOOTING.md) before device operations. A checksum cannot remove that shared-storage loading risk.

CC8 captures and renders audio. USB has priority over paired TLS Wi-Fi. Mac performs AEC/VAD/local interruption and local bilingual wake; MiniMax provides selected utterance ASR/TTS and Dot/Codex is the selected brain with owner-configured permissions. Camera input requires an explicit foreground request and a visible bounded preview. Home, manual Stop and loss of all authenticated transports stop capture; reconnect never rearms it.

Source candidates and the deployed installation are different. See [release.json](release.json), [CHANGELOG](CHANGELOG.md) and [validation evidence](docs/VALIDATION.md). Passing CI or synthetic model checks does not prove human accuracy, acoustic echo cancellation, physical USB/Wi-Fi handover, standby wake or 72-hour reliability.

Development starts with an isolated profile:

```sh
./cc8 setup --profile dev
./cc8 doctor --profile dev
.toolchains/cc8/dev-venv/bin/python tools/test_all.py
.toolchains/cc8/dev-venv/bin/python tools/quality.py check
```

JDK 21+ and OpenSSL are required for the complete offline suites. Device builds use the separate Android profile; voice runtime currently targets Apple Silicon macOS/Python 3.13. Linux CI covers portable tests and builds, not a verified Linux voice deployment. Existing signing material, credentials and pairing must remain private and unchanged. Starting a host service alone does not open capture.

[Fixed ASR scoring](docs/ASR_EVALUATION.md) reads supplied private text without recording/uploading it; [observation](docs/RUNTIME_OBSERVATION.md) only queries private health IPC. Their reports distinguish missing evidence and never automatically assert device acceptance.

Read [CONTRIBUTING](CONTRIBUTING.md) and [SECURITY](SECURITY.md) before sharing feedback. No recordings, photos, credentials, device identifiers, original firmware, signing keys or model weights are included. The complete project and character illustration are not assigned a new open-source license by publication; see [NOTICE](NOTICE.md). Do not assume a license grant for third-party character rights or downloaded dependencies.

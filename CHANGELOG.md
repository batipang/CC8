# Changelog

## Unreleased — failure-feedback-20261102.1

Backend 0.13.1; APK and wire protocol versions stay independent and unchanged.

- Split vision and speaker/pause lifecycle ownership behind explicit session
  ports, preserving foreground authorization and existing audio parameters.
- Fix a completion-condition/session-guard cycle during concurrent pause expiry
  and owner cancellation. Recheck generation before timeout cancellation so an
  old output cannot cancel a newer request. Add a bounded subprocess regression.

- Fix self-check retry to query the playback owner after extraction; verify the
  actual registered signal ignores stopped or busy sessions without capture.

- Bound read-only diagnostic IPC by one overall deadline, including slow
  fragmented replies, without changing model/tool retry behavior.

- Add `cc8 measure` for bounded content-free diagnostic runs, initial history
  exclusion, request deduplication, partial-coverage receipts and atomic private
  checkpoints. Collection never starts capture or model requests.

- Detect 大黄蜂 / Bumblebee locally before cloud ASR, with a bounded idle
  conversation window and a second worker-side authorization gate. Keep the
  same-utterance prefix buffer, continuous mode and existing stop/reconnect rules.
- Reuse Chinese stop-model weights through an independent native stream; pin a
  dedicated English BPE KWS archive after bilingual-model synthesis checks.
  Document model provenance, resource measurements and human acceptance gaps.

- Preserve stage-specific timeout, quota, authentication, connectivity and
  declined-approval failures instead of speaking Dot errors as normal answers.
  Keep completed task receipts after TTS failure; expose missing playback ACKs.
- Retain requests timeout/disconnect categories at all MiniMax transport entry
  points. Recovery uses fixed content-free guidance and never retries a task.

This candidate has not replaced the running installation.

## foundation-20261102.1 — source snapshot

Independent components: assistant APK 0.8.7 / code 32, host 0.12.5,
diagnostics APK 1.0.1 / code 2, wire protocol 2. See `release.json` and
`docs/RELEASES.md`; this snapshot does not assert device acceptance.

- Establish required Linux/macOS CI, pinned quality tools, redacted history
  scanning and complete fixed Python package inventories.
- Harden binary/JSON boundaries and fix TLS descriptor release while a reader
  is active; add seeded protocol and real TLS concurrency tests.
- Validate non-secret settings across startup, service and builds, keeping
  credentials, pairing and original signing material separate.
- Add isolated setup/doctor/run commands and an owner OS lease across checkouts.
- Check API-27 APK signatures and installed certificates before device writes.
- Centralize independent versions and compatibility; bind artifact receipts to
  source revision, manifest and hashes, with annotated-tag verification.

The running installation remains assistant 0.8.6 / host 0.12.2 until separately
deployed. Human voice, physical handover, standby wake and long-run acceptance
remain open in `docs/TODO.md`.

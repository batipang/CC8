# Changelog

## Unreleased — p0-review-20261102.1

Backend 0.13.7; APK 0.8.8 / code 33 and protocol 2 unchanged.

- Publish completed-clause playback state before waking its renderer, so a
  previous ACK cannot clear the next clause's active state.
- Serialize playback timeout/device/provider error cancellation and error UI
  with owner requests; recheck completion after acquiring the session guard.
- Keep an expired-pause status in the same owner transaction, preventing a
  late listening label from replacing a new request's thinking state.
- Add four bounded real-thread regressions reproducing five failures on the
  pre-fix controller. No device capture or production service change.

## p0-playout-20261102.1 — source snapshot

Backend 0.13.6; assistant APK 0.8.8 / code 33; protocol 2.

- Negotiate an optional first-signal playback-head receipt. Validate its clause,
  generation, frame bounds and host PCM signal index before recording one
  Mac-clock diagnostic stage. This is device progress, not acoustic audibility.
- Keep legacy peers, silence, pause/cancel and transport reset safe; a separate
  short publication lock preserves cancellation without holding wire I/O.
- Cover reader ACK-before-send-return and cancel/publication races, short writes,
  small final buffers, invalid/stale/duplicate receipts and Java playback logic.

Not deployed; human and physical P0 acceptance remains pending.

## p0-software-20261102.1 — source snapshot

Backend 0.13.5; APK and wire protocol versions stay independent and unchanged.

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

- Add a fixed 15-calibration/60-acceptance generic ASR corpus and private
  `cc8 score-asr` reports, with missing coverage, explicit field annotations,
  paired A/B and no recording, upload or automatic acceptance claim.

- Add bounded owner-process gauges to health and `cc8 observe` for private
  resource curves, runtime segments and explicit sampling/legacy-data gaps.
  Observation never starts capture, executes tasks or restarts services.

- Prepare a new owner-private installation receipt before device operations;
  reject public/symlink output folders and retain unconfirmed state on failures.
  A late report-save failure never automatically reinstalls the APK.

- Propagate MiniMax search failures as failed task receipts with classified
  recovery; never narrate a failed search as a successful normal answer or log
  arbitrary failed-progress payloads.

- Release capture and wake local playback waiters even if cancellation
  notification fails. Keep output-send errors in the transport stage, including
  initial playback and caption callbacks inside provider iteration.

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

# Changelog

## p0-vision-errors-20261103.1 — source candidate

Backend 0.14.5; assistant APK 0.9.2 / code 37; protocol 2.

- Keep camera acquisition, Dot analysis and TTS failures classified through
  vision cleanup, without replacing actual results with generic failure text.
- Whitelist camera error codes before metrics and desktop IPC; distinguish
  acquisition timeout from cancellation and add a shared vision error stage.
- Trace accepted vision requests before acquisition and serialize failure and
  completion against a newer picture. Preserve completed tasks when narration fails.
- Release camera controls while leaving actionable errors visible; keep explicit
  foreground authorization and cancel-then-begin protocol behavior.

Not deployed; running P0 baseline remains backend 0.13.7 / APK 0.8.8.

## p0-error-completion-20261103.1 — source candidate

Backend 0.14.4; assistant APK 0.9.1 / code 36; protocol 2.

- Publish text-request errors before clearing their live owner, and serialize
  failure publication and completion against a successor request. Keep ASR and
  literal-announcement failure publication within the same owner boundary.
- Preserve actionable errors after text/announcement cleanup, admit the terminal
  playback error following an owned cancel, and reject stale status/completion.
- Release playback controls on failure and show full recovery guidance in a
  readable dialog when the status is tapped. No automatic retry or new capture.

Not deployed; running P0 baseline remains backend 0.13.7 / APK 0.8.8.

## Unreleased — p1-saved-results-20261102.1

Backend 0.14.2; assistant APK 0.9.0 / code 35; protocol 2.

- Add explicit terminal-task result saving, restart-safe viewing/copy/delete,
  owner-private atomic storage, bounded cross-instance locking and no eviction.
- Keep ordinary history RAM-only; accept server-owned receipts only, never
  client-supplied success text. Stored snapshots do not restart jobs or approval.
- Isolate archive I/O, preserve uncertain-write semantics after disconnect,
  and release completed slots without clearing a successor refresh worker.
- Advertise optional UI capabilities and explain older backend limitations.

Not deployed; P0 baseline and original running installation remain preserved.

## p1-upgrade-preparation-20261102.1 — source candidate

Backend tools 0.14.1; APK 0.8.9 / code 34 and protocol 2 unchanged.

- Add offline source-version planning with both protocol directions, downgrade
  flags and optional existing AAPT/API-27 APK validation. Never assert installed
  signature continuity or deployment readiness from source declarations.
- Explicitly back up a validated non-secret profile and pinned original key
  references, verify integrity, and stage restore to a new private file only.
  Never read key contents, replace the live profile or manage devices/services.
- Verify synthetic restore roundtrips and root CLI under a minimal environment.

## p1-chat-handoff-20261102.1 — source candidate

Backend 0.14.0; assistant APK 0.8.9 / code 34; protocol 2.

- Add explicit, paginated metadata-only desktop chat discovery and selected-chat
  excerpt handoff in Settings. Read only the confirmed source's completed text,
  keep excerpts in RAM, and label the independent result with its source.
- Preserve native owner configuration and existing tool/approval boundaries.
  Do not resume/write desktop chats when shared-host access is unverified.
- Reset owned voice context when selecting/clearing a source, reject busy or
  stale operations, cancel subsequent reads and keep late interrupts scoped.
- Add result copy for manual desktop handoff; no automatic messages or capture.

Not deployed; P0 baseline remains host 0.13.7 / APK 0.8.8.

## p0-review-20261102.1 — source candidate

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

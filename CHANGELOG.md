# Changelog

## p0-pause-candidate-withdrawn-20261103.1 — default backend restored

Backend0.14.13 / original APK0.9.7/code42, protocol2 unchanged.

- Reject the twelve-frame pause candidate after both planned eleven-case live
  collections: it failed to retain all three baseline matching speech cases.
- Preserve every result, missing-coverage case and frozen decision rule; fewer
  reference-only pauses do not override speech regressions. Do not retune this
  holdout and call it independent acceptance.
- Restore the known backend without replacing APK/configuration/models. Remove
  the extra pause wait in default source; retain tail drain and receipt isolation.
- Add component checks preserving short-speech recognition handoff and immediate
  confirmed pause, alongside independent streaming stop and existing ACK tests.


## p0-confirmed-playback-pause-20261103.1 — rejected experimental candidate

Backend0.14.14, retained original-signature APK0.9.7/code42, protocol2 unchanged.

- Require twelve confirmed near frames within the current utterance before the
  first tentative playback pause. Reset on capture lifecycles; preserve ASR
  admission, acoustic thresholds, endpointing and independent streaming stop.
- Pre-register new synthetic-voice paired controls and keep the previously seen
  voice samples as development data, including their existing failures.
- Record the successful0.14.13/code42 deployment, three exercised resume cases
  and one unexercised late case. Q13 completed with real padding but still had
  a rejected short candidate pause; do not claim false-pause acceptance.


## p0-paused-tail-drain-20261103.2 — source candidate

Backend 0.14.13 / assistant APK0.9.7/code42; audio code unchanged from PR69.

- Wait for the explicitly opened owned Activity, retaining the existing USB
  target check, shell deadline and requested auto-voice flag.
- Preserve the first code40 deployment failure and successful code41 rollback:
  it stopped at complete pair readiness, before any audio fix validation. The
  script omitted the exact failure code; cold UI controls did not reproduce it.
- Isolate Endpoint/queue fixtures from both background workers after macOS CI
  exposed an unintended ASR consumer; retain the failed run and add an explicit
  no-consumer invariant. Real worker integration tests still start their workers.
- Running is the restored 0.14.12 / APK0.9.6/code41, not this candidate.


## p0-paused-tail-drain-20261103.1 — source candidate

Backend 0.14.13 / assistant APK 0.9.5/code40, protocol2 unchanged.

- Prime a resumed, fully-written short AudioTrack tail with bounded post-speech
  silence; preserve speech/reference/caption counts and require the real speech
  endpoint before play_done. Keep timeout, cancellation and write failures.
- Bind pause ACKs to their initiating trace after queue handoff and send pause
  with the original generation so a newer owner cannot be paused by a late event.
- Preserve 0.14.12 live results: 30 scripted questions executed, 29 playback
  completions, one retained failure; no extra admitted requests. Two successful
  resume controls and one late-tail failure remain distinct from software checks.
- Pass installer23 / host664 (4 private opt-in skips) / tooling100 / Java15,
  pinned quality checks and original-signature Android assemble/lint.
- Prepare a metadata-only code41 recovery of the existing 0.14.12 features.
- Original 0.14.12 / APK0.9.3 stays deployed until separate guarded validation.


## p0-diagnostic-deployment-20261103.2 — deployed diagnostic backend

Backend 0.14.12/source 0f2fbc7, original APK 0.9.3/code38 retained.

- Deploy the low-evidence admission and partial-trace lifecycle fixes in
  22.713 seconds, preserving the package, profile, voice, models and environment.
- Preserve the failed 0.14.11 preflight and its exact old controlled-test partial
  record. Verify its ownership for this migration only, with 12 guard checks.
- Keep new-instance maintenance strict and finish with capture/camera off.


## p0-partial-capture-lifecycle-20261103.1 — source candidate

Backend 0.14.12; original assistant APK 0.9.3/code38 and protocol2 unchanged.

- Cancel endpoint-owned partial voice traces on Home/stop, transport/session
  reset and fast stop; hand ownership to recognition only after queue admission.
- Finish discarded queued recognition traces without touching in-flight ASR or
  completed work. Keep strict pending-task checks in maintenance tooling.
- Pass installer23 / host660 (4 private opt-in skips) / tooling100 / Java15
  and pinned quality checks; five old-code regressions give nine assertion failures.
- Retain the failed 0.14.11 preflight: an abandoned trace from controlled C03
  blocked maintenance before any service or source change. Running stays 0.14.10.


## p0-weak-near-admission-20261103.1 — source candidate

Backend 0.14.11; original assistant APK 0.9.3/code38 and protocol2 unchanged.

- Reject unconfirmed render-risk ASR before creating another Dot/vision request,
  using the existing per-request near evidence; resume rejected tentative pauses.
- Keep stop/local-wake/idle paths and existing pause timing. Do not treat absent
  legacy evidence as zero or claim the false-pause budget has been met.
- Accept sentence-final punctuation on explicit short look commands while still
  rejecting quotes, negations and unrelated look-up requests.
- Linearize evidence-based admission with the existing session guard so a late
  ASR result cannot replace a newer request; cover rejected-tail UI recovery.
- Pass installer23 / host655 (4 private opt-in skips) / tooling100 / Java15 and
  pinned quality checks before deployment.
- The actual installation is 0.14.10/source e82c1cb; retain the two calibration
  coverage gaps and original failed runs, separately from software verification.

## p0-diagnostic-deployment-20261103.1 — deployed diagnostic backend

Backend 0.14.10/source e82c1cb, original APK 0.9.3/code38 retained.

- Deploy in 23.835 seconds with configuration/model/voice/package guards intact.
- Finalize the old 0.14.8 observation as cancelled: 5553.910 seconds, 93 successful
  polls, no errors. Its duration is not combined with another version.
- Start a separate bounded one-hour diagnostic observation, not final 72-hour
  acceptance. A continuous-query check captured a low-evidence admitted candidate.


## p0-voice-gate-evidence-20261103.1 — source candidate

Backend 0.14.10; original assistant APK 0.9.3/code38 and protocol2 unchanged.

- Add bounded per-utterance numeric capture observations and fixed ASR gate
  decisions to content-free diagnostics and the strict run collector.
- Preserve terminal outcomes and reject conflicting evidence, invalid counts
  or arbitrary content. Keep lightweight collection independent of audio imports.
- Record three completed 0.14.8 standby checks and the stopped continuous-query
  runs separately; the unintended admitted turn remains unresolved.
- Do not change acoustic/recognition thresholds or replace the live installation.

## p0-dns-deadline-20261103.1 — source candidate

Backend 0.14.9; original assistant APK 0.9.3/code38 and protocol2 unchanged.

- Bound MiniMax DNS wait and all returned TCP address attempts by the connection
  deadline, including ordinary CONNECT proxy host lookup and local bind names.
- Cancel the caller without waiting for the OS resolver. Limit outstanding OS
  lookups to four daemon workers with no queue, cache, callback or automatic retry.
- Keep TLS hostname verification, per-request pools and structured timeout errors,
  including wrapped proxy timeouts. Late DNS results cannot create a connection.
- The running 0.14.8 installation and its independent observation stay unchanged.

## p0-candidate-20261103.2 — deployed backend candidate

Backend 0.14.8, source bb7767b; original assistant APK 0.9.3/code38 retained.

- Deploy receive-path completion, turn linking, stage timings and initial Dot
  connection preparation without changing the APK, model, voice or dependencies.
- Preserve the first unsuccessful readiness check and confirmed rollback; the
  corrected bounded wait then confirmed the new idle pair in 22.374 seconds.
- Complete ten real idle backend recoveries, then start a separate version-bound
  0.14.8 observation. Three new standby rounds remain in progress.
- Record four existing native audio replays and four public human reference
  clips separately from CI, synthetic wake failures and field acceptance.

Earlier source-candidate notes below describe their state at the original merge.
Current deployment evidence and limits are in docs/CURRENT_DEPLOYMENT.md.

## p0-dot-preconnect-20261103.1 — source candidate

Backend 0.14.8; assistant APK 0.9.3 / code38; protocol2.

- Prepare the initial owned Dot connection and empty ephemeral context during
  explicit voice calibration, using the existing serial response queue.
- Do not submit a model turn or create a task, evict real queued requests, reset
  selected/existing contexts, or enable capture on reconnect.
- Keep preparation optional with a ten-second context creation budget. Failure
  disposes of the initial owned client without retrying; the next explicit
  request uses its normal handling. Failed preparation logging cannot become a
  fictitious answer failure or kill the response worker.

Not deployed; the version-bound 0.14.5 observation remains unchanged.

## p0-dot-timings-20261103.1 — source candidate

Backend 0.14.7; assistant APK 0.9.3 / code38; protocol2.

- Split Dot preparation, context reuse, turn acknowledgement, final-text arrival
  and authoritative completion on the Mac monotonic clock.
- Preserve receive times for pre-ACK notifications and separate them from task
  admission and audio consumption; do not emit synthetic model warm-up turns.
- Keep strict, content-free cumulative receipts and new/reused/unreported groups;
  older backends retain missing stages rather than invented zero latencies.
- Forward current provider metrics before router callbacks, preserving turn links
  when first-clause narration fails after a completed text or vision request.

Not deployed; the version-bound 0.14.5 observation remains unchanged.

## p0-dot-receipts-20261103.1 — source candidate

Backend 0.14.6; assistant APK 0.9.3 / code38; protocol2.

- Validate and admit exact-turn final receipts on the app-server receive path,
  independently of a yielded clause's synthesis or playback. Keep task completion
  when TTS fails or narration is stopped after the receipt.
- Share bounded final-text/phase validation with the speech consumer; reject
  changed answers, failed/malformed terminal events, stale turns and late tools.
- Unsubscribe before failure finalization and serialize task receipt admission
  against session cancellation, without adding provider threads or task retries.

Not deployed: the version-bound 0.14.5 observation remains running unchanged.

## Unreleased — version-bound observation tool

- Add optional `observe --expect-backend` validation on every health poll;
  missing/mismatched versions remain gaps and make the run partial.
- Record the expected version without copying arbitrary service strings.
  This CLI-only change does not restart or update the deployed backend.

## p0-candidate-20261103.1 — deployed candidate

Backend 0.14.5; assistant APK 0.9.3 / code38; protocol2, source cdfa4c2.

- Deploy the reviewed network-budget and error-lifecycle fixes with the original
  signer, configuration and pairing, keeping MiniMax ASR/TTS and Dot unchanged.
- Add content-free UI state/task status diagnostics and isolated 48-case error
  UI automation. The QA package is separate from the deployed assistant.
- Preserve the previous functional source and prepare a forward-version recovery
  package without uninstalling or clearing the original app.

Actual deployment and acceptance limits: [CURRENT_DEPLOYMENT](docs/CURRENT_DEPLOYMENT.md).
Complete P0 acceptance, wake-model improvements and long-run validation remain open.

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

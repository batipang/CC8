# Changelog

## Backend 0.14.79 — separate response acoustic fit failures

- Report bounded reasons for insufficient room-fit coverage, an unconfirmed
  model, or a failed prefix block. Keep response acoustic acceptance unchanged.

## Backend 0.14.64 — report captured voice onset to device pause

- Pair the first frame of the contiguous voiced run with the same applied
  pause receipt. Keep the existing detector-trigger metric separate.

## Backend 0.14.63 — measure false pauses during self-playback

- Expose pause and resume counters to the owner-only runtime check.
- Verify two timed self-playback windows with capture and playback receipts.

## Backend 0.14.62 — confirm routed operations before counting latency

- Record bounded web and terminal completion timestamps for the current request.
  Routed latency batches exclude answers without a matching completion.

## Backend 0.14.61 — separate web and local tool latency samples

- Add fixed public web lookups and read-only local calculations to the
  synthetic timing suite. Require evidence of the intended route.

## Assistant 0.9.34 — record when stop updates the playback visuals

- In bounded capture tests, associate the first cleared caption, progress and
  mouth animation update with the same player stop and device clock.

## Backend 0.14.60 — time the stop command on the device clock

- Pair the capture frame that triggered the local stop detector with the
  device pause-and-flush receipt for the same cancellation.

## Backend 0.14.59 — automate the local stop-command check

- Expose the local stop detector's availability and hit count. Require one
  fresh hit and the device's applied stop receipt in the opt-in test.

## Assistant 0.9.32 — observe an applied player stop during test capture

- Record a stop for the active route and player only after Android AudioTrack
  has completed pause and flush. Expose one bounded receipt during the
  explicitly timed test capture.

## Backend 0.14.58 — deliver device cancellation after a provider error

- Attempt both provider cancellations and send the invalidated generation to
  CC8 even if provider cleanup raises. The device can then discard queued PCM.

## Backend 0.14.57 — corroborate synthetic dialogue response in room audio

- Allow a one-shot passive measurement for a fixed local-wake QA question.
  Correlate the answer's render reference against device microphone audio
  without pausing the active voice pipeline or saving audio.

## Backend 0.14.56 — bound proxy CONNECT and origin TLS together

- Keep the original connection deadline through CONNECT headers and the TLS
  handshake with the origin, then release it for the response read phase.

## Backend 0.14.55 — measure trailing capture before voice endpoint

- Record the device capture interval between the last frame marked voiced and
  utterance handoff when the frame clock is contiguous. Summarize accepted
  utterances separately from host ASR and playout timings.

## Backend 0.14.54 — pass the remaining connection timeout to TLS

- Recompute the socket timeout after TCP connects so the first TLS handshake
  inherits the time remaining after DNS and TCP.

## Assistant 0.9.31 / Backend 0.14.53 — stop rejected microphone capture

- Negotiate a capture rejection event bound to a single recording ID. Stop
  AudioRecord when its host processing fails to initialize, even if the same
  recording reconnects on the paired route. Ignore stale route or capture IDs.

## Backend 0.14.52 — keep working echo processors on rebuild failure

- Publish the replacement echo processors and alignment only after the new
  pair is warm. Release partial allocations and keep the prior pair on failure.
- Publish host capture only once its echo processors have initialized.

## Backend 0.14.51 — treat a punctuated wake name as wake-only

- Keep listening when a locally accepted wake name is followed only by
  punctuation. Preserve numeric signs and decimal points in a real question.

## Backend 0.14.50 — match response timing to a confirmed clause

- Record the progress and completion timestamps of the same confirmed clause.
- Keep older confirmation counts and exclude timings without the paired receipt
  from confirmed-response percentiles.

## Backend 0.14.49 — fence buffered audio at the wake boundary

- Keep audio already received before waiting for local wake out of keyword
  detection and speech admission while retaining it for echo cancellation.

## Backend 0.14.48 — preserve wake activity at the standby boundary

- Serialize the standby decision and screen events with local wake and touch.
- Use the same voice-work checks for standby and the wake window, including
  speech in progress and queued recognition or replies.

## Backend 0.14.47 — integrated P0 device-test candidate

- Include the separated device-event and playback-pause owners in one
  candidate with the six-phase device suite and runtime-aware cleanup.
- Keep assistant 0.9.30 / code 65 and protocol 2 for this candidate.

## Backend 0.14.46 — bound MiniMax search retries

- Retry read-only MiniMax search at most once on HTTP 502, 503 or 504 without
  a Retry-After header. Both attempts share the original network budget and
  turn cancellation.

## Assistant 0.9.30 — discard obsolete playback UI updates

- Check playback ownership both when publishing a caption or state and when
  the main thread displays it. Cancel, replacement and a newer pause/resume
  update invalidate callbacks still queued.

## Assistant 0.9.28 — keep paused playback visuals in sync

- Derive mouth, waveform and caption progress from one playback head sample.
  Freeze the caption and close the mouth when AudioTrack applies a pause.
  Late timing updates take effect when playback resumes.

## Backend 0.14.45 — verify microphone stop across a complete test outage

- Add an owner-only test command that closes both assistant transports and
  declines reconnection for ten seconds. The device test verifies both ends
  stop recording and that USB priority returns without restarting capture.

## Assistant 0.9.26 — verify an active microphone across transport changes

- Expose the native capture epoch only during an explicitly bounded test capture.
  The optional logical handover check requires the same epoch and deadline,
  fresh microphone frames on both routes, and exact switch counts.

## Backend 0.14.44 — check local wake during device playback

- Expose cumulative local wake acceptances and voice ASR attempts in owner
  health. Check each fixed Chinese and English name announcement while
  waiting for wake, including a 20-second observation after it finishes.

## Assistant 0.9.24 — verify the synthetic interruption transcript

- During explicitly bounded device tests, record whether the received transcript
  matches the fixed interruption query. The test requires a match from its new
  response generation. The device reports only a boolean and generation.

## Assistant 0.9.22 / Backend 0.14.43 — device pause timing

- Record estimated microphone trigger frame to applied Android pause latency
  when the device advertises applied receipts. Aggregate only accepted
  voice turns and keep the existing Mac round-trip measure separate.

## Assistant 0.9.20 — confirm playback pause and resume

- Acknowledge only applied AudioTrack pause/resume commands. A pause during
  initialization is applied before the first PCM write, and a withdrawn
  pending pause emits no device receipt.
- Route AudioTrack control failures through the existing playback error event.

## Backend 0.14.42 — local wake playback references

- Preserve English word boundaries and clause order when suppressing a wake
  name in recent playback. Retain the name-specific guard after completion
  even if the microphone stream has no matching render reference.

## Backend 0.14.41 — playback receipts for each clause

- Count clauses whose validated playout and completion receipts identify the same playback.
  Preserve existing first-stage timing and compatibility with older device receipts.
- Add `verify-device --multi-clause` for a fixed two-clause announcement with
  continuous microphone-off checks and a bounded private receipt summary.

## Local wake research — phase-matched warmup pair

- Complete 2400 fixed trials across original/patched native extensions, four
  chunk-aligned warmups, six offsets and production/evaluation packet sizes.
  Keep all PCM hashes and reproduce each native's 150 historical control cells.
- No fixed warmup passes every positive. Preserve misses and reject deployment;
  the bundled patch improves some conditions but is not a single-factor proof.
- Retain separate reports, plans, parameter/code identities, seven harness
  safeguards per script and independent audits. Keep runtime and its ongoing
  observer unchanged; no capture, cloud calls or audible synthesis.

## Local wake research — raw cached Whisper comparison

- Verify an existing fixed-revision MLX checkpoint and 30 separately pinned
  research wheels; retain original runtime, models, configuration and observation.
- Complete 225 offline decodes on the same 25 public fixtures and PCM hashes.
  All configurations falsely trigger on Bumble by; a fixed name hint also
  triggers Bubble bee. Reject this candidate without changing labels or aliases.
- Preserve model/plan/source/report identities, seven harness checks and an
  independent result audit. Whole-utterance decoding is not streaming wake or
  human acoustic acceptance; P0-03 remains open.

## Automatic validation — TLS UI, supervisor recovery and observation

- Complete 48/48 isolated physical QA error-detail/recovery/stale-event cases
  with 30 verified loopback TLS requests; preserve the earlier missing-bundle
  failure. Remove only the QA APK and verify the original assistant idle/off.
- Pass ten fixed supervisor faults/adoptions and four subsequent no-capture
  device checks. Keep the same backend/APK/models; do not generalize repeated
  idle faults to sleep/login, arbitrary tools or physical USB unplugging.
- Start a separate version-bound 72-hour read-only observer after planned
  faults and QA. Record launch identity and minimal-PATH preflight; still running,
  with no inherited observation time or full P0 completion claim.
- Retain the real rejected/resumed echo pause. Its five credible frames do not
  prove moving the ASR floor to the pause path would fix it; no threshold change.

## Automatic failure UI — recheck production ownership before takeover

- Revalidate the same idle production backend after fixture generation, signer
  inspection and QA installation, before taking the foreground UI.
- If the owner resumes capture, camera or another foreground activity, stop;
  remove only the newly installed identical QA package without reopening the
  original UI when takeover has not begun. Reject replacement backend identities.
- Record successful ownership boundaries. Four regressions reproduce four
  old-driver assertion failures, with no fixture errors. No production APK,
  runtime, model or audio-setting changes; ADB handoff is not atomic.
- Verify the ignored installer bundle and tracked helper source before fixture
  generation. A real QA attempt stopped on the missing local bundle before UI
  takeover; preserve that failure and test the earlier prerequisite rejection.

## Automatic failure UI — optional verified loopback TLS fixture

- Add explicit `verify-failure-ui --http-fixture loopback_tls`: 30 ASR/TTS cases
  use real certificate-verified HTTPS, request budgets and controller failure
  handling, with one counted server request per case. Keep 18 Dot cases mocked
  and recovery UI-only; generate only the selected cases before UI replay.
- Use ephemeral loopback ports/certificates and fixed invalid credentials;
  disable proxy inheritance, remote POST targets and redirects. Keep production
  endpoint validation, credentials, configuration and runtime unchanged.
- Bind the I/O mode and wire count into corpus identity and include the fixture
  source hash. Keep default fake mode and standard-library dry plans offline.
- Pass seven new regressions, including trust rejection and cleanup. New-mode
  physical QA remains pending; do not inherit the earlier fake-I/O device pass.

## Automatic acoustic checks — candidates and admitted dialogue

- Distinguish VAD diagnostic records from admitted dialogue. Require the fresh
  question to complete playback and retain the 20-second observation; an extra
  admitted generation, brain start, accepted gate or task still fails at once.
- Count rejected and pending candidates separately. Do not ignore unknown or
  cancelled terminal records; unresolved candidates and unresumed acknowledged
  pauses cannot pass the final observation boundary.
- Keep raw record counts and actual pause/resume receipts visible. A completed
  answer is not evidence of pause-free playback; retain the original failed
  standby report and missing post-playback window without replay or relabelling.
- Test-tool change only: no runtime, model, audio threshold, APK or user-setting
  modification. New-mode physical validation remains separate.

## p0-supervisor-log-recovery-20261104.1 — deployed diagnostic pair

Backend0.14.17, frozen source1ab67a0; original-signature APK0.9.13/code48 and
protocol2 retained. PR81 and main each pass all eight CI checks.

- Keep optional log write/flush failures out of transport activation, device
  handshake and workers after a supervisor's stdout pipe loses its reader.
  Preserve JSON serialization failures and healthy output; do not add log files.
- Reproduce the real closed OS pipe and connection path: three old-code
  assertions fail, zero fixture errors; all five candidate cases pass.
- Preserve the actual code48 standby attempt that stopped before capture when
  its maintenance reopen lost communication. Its legacy failure label referred
  to capture, but independent device/backend checks showed both microphones off.
- Restore the original backend safely in 12.736 seconds before further work;
  stop the incomplete observer/diagnostic collectors and retain their reports.
- Deploy the tested backend in 23.483 seconds after a separate 6.877-second
  preflight, preserving APK, configuration, models and native dependencies.
- Pass one actual supervisor fault/adoption check in 7.747 seconds, followed by
  four no-capture checks covering the original UI-reopen/reconnect failure,
  announcement/caption receipts and logical Wi-Fi/USB handover.
- Preserve the first new-version standby failure: eight checks pass, including
  a real 908.424-second idle; acoustic counting stops on an extra raw voice
  record. Independent numeric receipts show one completed admitted answer and
  one rejected/resumed echo candidate, not a second admitted dialogue. Keep the
  actual pause and missing 20-second observation visible; no rerun or green
  relabelling, and remaining standby rounds stay unstarted.

## Automatic supervisor recovery — explicit fault and adoption checks

- Add `verify-service --fault supervisor`; keep backend-only faults as default.
  Require the exact owner LaunchAgent PID, complete idle gates, a replacement
  supervisor and a ready write after the fault. Never start capture or replay work.
- Recognize service.py's existing surviving-backend adoption only with parent1,
  fresh private ready metadata and the matching launchd-owned supervisor.
- Re-sample when a replacement appears between process snapshots; still reject
  duplicates. Pass 21 targeted checks, including minimal-PATH plan execution.
- Preserve the initial real-device checker failure and separately confirmed
  recovery; do not repeat the fault merely to obtain a green combined report.

## p0-bounded-test-capture-20261103.1 — deployed diagnostic pair

Original-signature APK0.9.13/code48; backend0.14.16 and protocol2 unchanged.
Frozen deployed source e689a1a; previous code46 results retain their own scope.

- Bound explicitly requested automatic capture on the device, independently of
  a Mac runner's finally block. Drop reads that cross the device-clock deadline;
  a per-record watchdog can stop a blocked native read and releases with its worker.
- Keep normal opening/manual voice unlimited. Reject malformed test limits;
  never retime an existing recording, extend on reconnect, or stop a replacement.
- Require the advertised device capability before test capture. Ordinary smoke
  capture requests 180 seconds, real standby 1200 seconds; no remote microphone IPC.
- Compile actual recording/finally/opening paths: eight targeted cases pass;
  preserve the interrupted physical reference test and separate cleanup proof.
- Reserve code47 for the previously prepared original-signature recovery package.
- Pass actual normal installation and complete pair readiness in 51.849 seconds;
  forward recovery APKcode49 is prepared and unused. Preserve configuration,
  model/native dependencies, original signature and user data.
- Verify device expiry after killing the owned Mac test child, then separately
  verify the original deadline survives live Wi-Fi/USB logical handover. Keep the
  initial handover-wait failure and independent cleanup report unchanged.
- Pass four no-capture device checks. Cancel the old code46 observation for this
  update; its 4062.472 seconds are not a complete or cumulative 72-hour result.

## Automatic acoustic checks — evidence and post-playback observation

- Preserve fixed content-free stage counts when an acoustic check fails; do
  not confuse a completed Dot task with a completed spoken answer.
- Observe 20 seconds after playback and fail on an extra voice request,
  cancellation, capture loss or a changed backend. Never replay the stimulus.
- Add five offline regressions: seven old-code assertion failures, zero fixture
  errors; 29 targeted and 729 host tests pass after the change.
- Record the actual two passed / one failed standby rounds and split active
  backend-fault/follow-up evidence without relabelling failed reports.
- Keep deployed backend0.14.16 / APKcode46 and all audio settings unchanged.

## p0-workspace-action-ownership-20261103.1 — deployed diagnostic pair

Backend0.14.16 / original-signature APK0.9.11/code46, frozen source867ff19.

- Deploy the tested MiniMax business-failure classification, serialized LAN TLS,
  interrupted-result notices and workspace callback/action ownership guards.
- Verify clean build/source-tree equivalence, original API27 signature, strict
  idle/configuration/model/dependency gates and a prepared forward-version
  recovery artifact. Preserve the initial report-directory guard failure.
- Pass complete pair readiness and stability in 54.004 seconds, then four real
  no-capture device checks in 40.557 seconds. Recovery APKcode47 remains unused.
- Complete 10/10 real idle backend SIGKILL/recovery cycles in 292.861 seconds,
  with identity/idle guards and no capture or model tasks. Keep active-work,
  full-service and OS sleep/login acceptance separate.
- Cancel the old version-bound observer for this actual update; preserve its
  incomplete window and disconnected samples. Do not combine version durations.
- See [current deployment](docs/CURRENT_DEPLOYMENT.md) for limits and follow-ups.

## p0-workspace-action-ownership-20261103.1 — client source candidate

Assistant APK0.9.11/code46; backend candidate0.14.16 and protocol2 unchanged.

- Bind task/reminder lists, forms and save/delete confirmations to the route
  that supplied their data. Reject old dialogs with fixed verification guidance.
- Check ownership atomically at the send boundary; a failed workspace enqueue
  must not retry through the audio transport fallback onto another activation.
- Bind saved-result responses and chained requests; an old button must not
  close a newer pending request. Preserve current explicit operations/receipts.
- Compile actual UI/send methods and the complete saved-results dialog: 18
  targeted cases, 16 old-code assertion failures and zero fixture errors.
  Production stays unchanged; see [workspace actions](docs/WORKSPACE_ACTION_OWNERSHIP.md).

## p0-workspace-route-ownership-20261103.1 — client source candidate

Assistant APK0.9.10/code45; backend candidate0.14.16 and protocol2 unchanged.

- Bind workspace/link callbacks to their connection activation and recheck
  ownership when applying queued UI changes. Invalidate on pause, loss and
  route selection, including returning to the same USB Link after Wi-Fi.
- Clear old workspace revisions on backup takeover even without an intervening
  disconnected callback; preserve uncertainty about known unfinished work.
- Compile actual lifecycle, dispatch and Activity callbacks under controlled
  parse/queue interleavings: eight old-code assertions fail, ten candidate
  checks pass. No capture, task replay or wire protocol changes.
- Keep production unchanged; see [workspace ownership](docs/WORKSPACE_ROUTE_OWNERSHIP.md).

## p0-interrupted-task-notice-20261103.1 — client source candidate

Assistant APK0.9.9/code44; backend candidate0.14.16 and protocol2 unchanged.

- Preserve a RAM-only uncertainty hint when a connection is lost with a known
  running task, before clearing old workspace arrays. Keep authoritative terminal
  receipts separate and do not fabricate outcomes or replay work.
- Keep the hint over empty reconnects/reused IDs, let the owner view current tasks
  or dismiss the hint without sending a request, and preserve the approved layout.
- Compile real Activity callbacks in six lifecycle regressions; four assertions
  fail on the old callbacks, with no fixture errors. Pass full local checks and
  Android release compilation/lint. Complete six isolated device UI checks and
  verify rebased QA package entries/signatures for evidence reuse; production
  deployment stays open.
- Retain the real isolated active-Dot parent-loss observation and first fixture
  guard failure. A surviving first file effect is not transaction rollback.

## p0-serialized-lan-tls-20261103.1 — backend source candidate

Backend0.14.16; original APK0.9.7/code42 and protocol2 unchanged.

- Serialize native SSL operations on authenticated LAN channels using
  nonblocking calls and readiness waits outside the lock, retaining deadlines,
  partial-write correctness and cancellation. USB and provider HTTPS stay unchanged.
- Reproduce overlapping real TLS entry points with a guard before unsafe native
  access; add encrypted duplex, backpressure and cleanup regressions.
- Make the USB handover fixture report presence and answer valid heartbeats.
  Preserve PR74's original macOS EOF failure; its exact SSL cause is unconfirmed.
- Keep the deployed assistant and its observer unchanged. See
  [LAN TLS concurrency](docs/LAN_TLS_CONCURRENCY.md) for evidence and limits.


## p0-minimax-business-failures-20261103.1 — backend source candidate

Backend0.14.15; original APK0.9.7/code42 and protocol2 unchanged.

- Classify explicitly documented MiniMax business failures independently of
  HTTP200, preserving HTTP/explicit-kind precedence and generic unknown cases.
- Reject malformed status envelopes and non-integer codes before releasing ASR
  text, JSON or stream events; preserve optional status compatibility and require
  explicit integer-zero status for search results.
- Verify no task dispatch after ASR failure, completed receipts surviving late
  TTS quota errors, and recovery only on the next explicit request. Keep failures
  content-free and add no retries or provider/configuration changes.
- Add verified loopback TLS controls for all three HTTP paths. Real MiniMax
  faults/device UI remain separate; deployed backend0.14.13 and its observer stay.
- Separately complete one real normal TTS-to-ASR-in-memory compatibility probe
  and one JSON search with the unchanged owner configuration/voice. Save no audio
  or transcript, play nothing and leave the deployed instance/observer untouched.


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

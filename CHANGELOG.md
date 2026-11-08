# Changelog

## Backend 0.15.57 — complete Chinese wake candidate review

- Add explicit `sensevoice_complete` mode, off by default: hold existing primary Chinese keyword hits until a bounded complete local utterance can veto explicit confusables. Reuse the existing WebRTC speech bit without lowering ordinary ASR or interruption gates. MiniMax ASR/TTS, owner Dot and APK88 remain.
- Keep screen-idle decisions behind a current Chinese name candidate while its weak utterance is collected; publish completion and queue ownership together, without letting unowned background speech hold the screen on.
- Bind completed candidates to one-use generation and capture receipts; invalidate them across playback, cancellation, capture gaps and transport changes. Prevent the parallel ordinary endpoint from borrowing a new wake and submitting the same question twice.
- Bound and isolate the local model child, distinguish unavailable inference from a valid non-veto, show current-only recovery guidance, check selected weights in doctor and erase owned candidate PCM when consumed or discarded.
- Verify 544 pinned digital conditions with the actual candidate capture/controller and local child; preserve all original positives without new false triggers. This replays pinned keyword hit frames, not physical or human wake acceptance. Include prior APK88 connection field evidence in this functional batch.

## Backend 0.15.56 / APK 0.9.51 — fresh connection diagnostic receipts

- Add explicit owner-local `diagnose.py --connection` export over the existing authenticated USB or Wi-Fi route. Query status once with a bounded wait; old peers, missing replies and disconnected devices remain unknown. Copy only validated device-local states and counters, excluding text and extensions.
- Expose visible connection-dialog observations in the existing native UI status dump, checking the actual TextView against its associated rendered snapshot. Do not substitute a newly computed tick for the displayed state, retain dismissed presentation or start capture for inspection.
- Include APK86 deployment and partial field results. Preserve the failed live UI read separately from confirmed recovery and lifecycle checks; actual sleep/login and full P0 acceptance remain open.

## APK 0.9.49 — observed connection availability

- Show first-connection waiting, the current observed outage and the last recovered gap in the existing connection dialog, refreshing only while it is visible in the foreground.
- Keep availability observations under route ownership: a ready fallback is continuous, failed/stale activations cannot create recovery, and background time is not counted. Export only bounded device-side durations and fixed labels.
- Preserve backend 0.15.55, providers, layout and capture rules. Include its deployed four-case cancellation evidence in this functional batch; actual sleep/login and physical switching remain separate.

## Backend 0.15.55 — scoped background terminal cancellation

- Keep asynchronous turn interruption bound to its original RPC and thread, then terminate only background terminal handles matched to command items observed in that turn. Never clean the whole thread or replay an unconfirmed operation.
- Serialize handle cleanup with new-turn admission; an overtaken callback cannot terminate a later turn's reused server handle. Completed work and intentionally retained background services remain separate.
- Make cancelled task receipts state that waiting stopped and tools may still be running. Include backend54 deployment and the failed live cancellation checks without claiming full acceptance.

## Backend 0.15.54 — cancellation belongs to its request

- Match task and vision cancel buttons while holding the session guard through cancellation and status publication, preventing an older button from cancelling a newer owner request.
- Recheck generation before provider cancellation and serialize brain/speech cancellation with request admission. A superseded cleanup cannot interrupt a newer Dot turn or MiniMax request.
- Preserve current-owner cancellation, completed work receipts, original APK84, providers and capture rules. Include prior backend53 deployment evidence and rejected offline audio candidates in this functional batch.

## Backend 0.15.53 — deferred signals and startup resource ownership

- Make stop/self-check signal callbacks record intent without acquiring resource locks. Process them at main-loop boundaries, give Stop priority and reject self-checks belonging to an old generation/capture epoch.
- Run owned cleanup after partial startup and unexpected loop failures, preserving the original cause when cleanup also fails. Do not dispatch a newly dequeued packet after Stop.
- Release RPC stdin under the writer lock; let stdout/stderr readers close their own pipes after draining admitted receipts. Clean up partial reader startup and prevent reuse of the failed client.
- Unlink an announcement socket only when its device/inode matches this instance's successful bind. Preserve rejected files and replacement sockets during startup cleanup.
- Retain original APK84, provider selection and capture rules. Carry the previous backend52 deployment and normal lifecycle evidence in this batch; login/sleep and physical/acoustic acceptance remain separate.

## Backend 0.15.52 — finish tasks and owned cleanup after failures

- Preserve the original speech/task failure and finish its receipt even if closing the response iterator fails; keep already completed work completed.
- Attempt both provider cancellations and close the owned Dot RPC even if an earlier cancellation raises. Propagate the first failure after all independent cleanup actions.
- Apply the same rule to the registered backend stop handler so one failing stop does not skip IPC, transport, brain, speech or channel cleanup. A failed channel shutdown still attempts close.
- Retain expected disconnected-I/O handling, provider selection and capture rules. Include the prior 0.15.51 deployment and successful fixed English field result in this functional batch.

## Backend 0.15.51 — complete-utterance local wake verification

- Supplement the existing real-time wake paths with a fresh, bounded English keyword stream on completed speech while waiting. Keep the current model, eight decoder paths, competitor, thresholds and providers.
- Authorize the same utterance for ASR only after a local keyword hit; ordinary non-keyword speech remains local and does not invoke Dot.
- Bind candidates to capture start, session generation and wake serial. Stop, gaps, handover, a newer wake and playback risk revoke pending checks.
- Keep recheck counts, hits, errors and duration in health without retaining audio or text. Offline/native evidence and field limitations are recorded in docs/UTTERANCE_LOCAL_WAKE.md.

## Backend 0.15.50 — raw English wake-input diagnosis

- Add bounded, opt-in pre-AEC input statistics to the fixed English wake probe, alongside its existing processed inputs.
- Replay a matched raw window through an independent local detector even when processed inputs fail to match. Keep the existing processed-pair replay separate.
- Mark missing or playback-risk raw frames as a gap; never compact them into a complete window. Clear buffers on completion, failure, cancellation and wake.
- Validate the new receipt without audio content while retaining older two-stream reports. Live wake models, thresholds and ASR/TTS providers are unchanged.

## Backend 0.15.49 / APK 0.9.47 — bind host output to activation

- Negotiate host-to-device envelopes in addition to the existing microphone/control envelope, preserving legacy protocol-2 fallback explicitly.
- Freeze output ownership before waiting for the writer; reject old bound or unwrapped application packets on the device, including LAN configuration.
- Order role changes across sockets with a process identity and monotonic serial, rejecting late/duplicate activations while allowing a fresh host generation.
- Reconfirm a surviving route after an uncertain activation write, retain unsent screen intent and discard microphone ingress until role confirmation. Explicit Stop still prevents rearming.
- Expose only negotiated version flags; keep identifiers and content out of status exports. Physical/acoustic acceptance remains separate.

## Backend 0.15.48 / APK 0.9.45 — microphone route admission

- Negotiate per-activation device-to-host envelopes for controls and audio, fencing old queued packets even when reusing the same socket.
- Discard microphone packets until the current recording is registered on the selected route; preserve explicit Stop and the existing recording identity.
- Recheck native queue ownership before writing, keep already admitted packets stamped with their original token, and avoid holding the route lock over socket I/O.
- Discard reads that straddle activation and clear paired conversion/partial-packet history on the capture worker.
- The envelope requires both updated peers. Legacy peers keep protocol-2 compatibility without the new wire freshness guarantee; physical handover and acoustic acceptance remain separate.

## Backend 0.15.47 — reset stop-keyword history on handover

- Start a fresh local stop decoder stream when the capture route changes, preserving session-level counters.
- Prevent a partial command from the previous route from cancelling a later answer; a complete command on the new route still works.
- Keep same-channel continuity, voice thresholds, microphone ownership and the ASR/TTS providers unchanged. This does not establish a fix for TV-background pauses or all queued-audio handover cases.

## Backend 0.15.46 — received microphone signal quality

- Count accepted 16 kHz microphone PCM before host DSP, including near-full-scale samples, zero samples, peak, RMS, DC offset and longest clipped run.
- Exclude the paired reference, stale captures and invalid frames; split runs at read, sequence and device-clock discontinuities.
- Keep only scalar evidence, revoke late updates on Stop/reset, and expose copies through health and private status reports.
- These counters do not measure device ADC saturation, acoustic SPL or recognition accuracy; input gain and voice admission stay unchanged.

## Tooling — active capture format evidence

- Require optional explicit mono/paired expectations against both peers' actual capture metadata, including after handover.
- Keep legacy reports unmeasured, reject fallback as paired acceptance, and recheck every phase during offline audit.

## Backend 0.15.45 / APK 0.9.43 — optional paired capture reference

- Negotiate an opt-in same-grid microphone/reference stream, preserving mono for peers without the capability.
- Downsample selected input channels with a causal anti-alias filter, retain partial reads, and send bounded interleaved logical frames.
- Keep playback ownership on its existing digital timeline while AEC can use the paired candidate reference.
- Preserve capture cancellation, foreground limits and strict fallback/handshake boundaries; report the active frontend separately.
- Keep hardware-clock and real-time acoustic acceptance distinct from component measurements and offline tests.

## Diagnostics 1.0.6 — require a configured USB data link

- Clear the transient analysis when USB loses data configuration even if the cable remains connected.
- Expose connected/configured readiness and observed link losses separately for physical validation.

## Diagnostics 1.0.5 — bounded RAM-only reference analysis

- Add a separately armed foreground reference-analysis button and ordered USB-shell reads of transient mono or four-channel PCM.
- Expire or clear unread audio on Stop, background, replacement and failed transfer, without writing capture files.

## Diagnostics 1.0.4 — compare inputs with a known reference

- Add bounded RAM-only block-mean comparison with known reference audio, including silent controls and competing peaks.
- Report per-channel DC and AC levels separately; keep missing and cancelled comparisons explicit.

## Diagnostics 1.0.3 — compare input channels during reference playback

- Add optional device reference playback to bounded channel statistics.
- Report its requested mode and observed playback head alongside channel metrics.
- Read the visible diagnostic version from the installed package.

## Diagnostics 1.0.2 — inspect input channel signals

- Add foreground six-second statistics for two- and four-channel input.
- Report per-channel levels, correlations and duplicate samples without saving audio.

## Backend 0.15.44 — enforce the deadline after receiving a Dot reply

- Recheck the shared request budget after the reply queue returns.
- Report an expired turn-start receipt as an unconfirmed submission.

## Backend 0.15.43 — report playback reference recognition separately

- Record how long the playback reference ASR attempt takes on its own voice
  candidate. Primary recognition timing and playback ownership stay separate.
- Preserve the fixed read-only omission flag in diagnostic exports when it
  accompanies a matching tool recognition comparison.

## Backend 0.15.42 — keep echo reference errors with their candidate

- Record failures of reference recognition on the unadmitted utterance, even
  between playback clauses, so they do not mark the current answer as failed.

## Backend 0.15.41 — resume playback after a failed echo reference check

- When reference recognition fails before a tentative interruption is confirmed,
  withhold the new request and release only its own playback pause. Ignore a
  late result after the user has stopped the session.

## Backend 0.15.40 — reap supervised children without an argv lookup

- Use the supervisor’s own child handle for liveness and shutdown. Temporary
  argv unavailability no longer abandons a live worker or triggers a restart.

## Backend 0.15.39 — report correlation scores for acoustic timing misses

- Include bounded correlation scores for unmatched windows and failed leading
  blocks without changing the acoustic timing acceptance criteria.

## Backend 0.15.38 — show when confirmed speech was not understood

- Use visible retry guidance when a confirmed near-end utterance yields no
  transcript. Leave capture and the current conversation active.

## Backend 0.15.37 — expose interruption timeout feedback

- Show the reason when an interruption timeout stops the original playback.
  Preserve the existing cancellation order and leave capture active.

## Backend 0.15.36 — expose overlong-utterance guidance on the retained APK

- Show the retry message through the existing visible error detail label. A
  rejected long utterance leaves capture ready for the next separate request.

## Backend 0.15.35 — withhold overlong continuous utterances

- Keep microphone and playback reference buffers bounded while waiting for a
  normal endpoint at the capture limit. If speech crosses the bound, discard
  the whole utterance and its continuation up to the next clear pause.
- Report the rejection before recognition. Resume only playback paused by
  that capture.

## Backend 0.15.34 — recognize workers in paths with spaces

- Read argument boundaries from the OS before accepting a worker's PID. The
  launcher and service verifier can identify the real script in a workspace
  path with spaces, including when a different process has the same flattened
  command line.

## Backend 0.15.33 — report live English wake stream positions

- In the existing opt-in fixed English fixture probe, record each live stream's
  submitted sample position before the first observed frame, plus any English
  keyword results rejected by the render guard in the sampled window.
- Keep the reports audio-free, accept older probe receipts, and preserve live
  wake decisions and the independent fresh-stream replay.

## Backend 0.15.32 — recover safely from worker PID write failures

- Reap the new child under the shared launcher lease if PID publication fails.
  The login supervisor backs off and can recover when storage is writable.
- Publish the backend's own PID before constructing providers or transports.
  Preserve the smoke-only entry and keep diagnostics free of raw error paths.

## Backend 0.15.31 — classify the fixed tool qualifier omission

- On an explicitly enabled same-input comparison, report whether the normalized
  primary transcript differs only by the omitted read-only qualifier. Preserve
  the legacy comparison object and keep the admission rules unchanged.

## Backend 0.15.30 — diagnose competing playback windows

- Keep the two rounded correlation scores when a fixed response probe finds
  ambiguous playback windows. They remain scoped to the owned response and
  cannot confirm an onset or contribute to latency statistics.
- Keep older receipts valid and leave the matcher, capture lifecycle, APK and
  device protocol unchanged.
- Recognize the fixed tool answer's exact terminal provenance suffix while
  still checking the whole remaining answer and independent tool evidence.

## Backend 0.15.29 — consume the fixed tool reservation on empty ASR

- Close the eligible fixed-tool turn when ASR returns no words, so a later
  utterance cannot replace the reserved sample or dispatch its tool.
- Keep empty transcripts as a guarded failure. When the owner enables paired
  diagnosis, compare the same input locally and retain only fixed categories.
  APK and protocol are unchanged.

## Device checks — retain a safe cleanup failure phase

- Identify which cleanup phase was unconfirmed without exporting device/IPC
  error bodies or obscuring the original check failure. A replacement runtime
  still blocks old device actions and takes precedence in the receipt.
- Keep existing stop/reopen checks and device actions unchanged. Record the
  0.14.88 field run and separate capture-off foreground recovery without
  rewriting the original acoustic or cleanup result.

## Backend 0.14.88 — causal transfer diagnostics for known-speech QA

- Add an opt-in pre-stimulus boundary to the known-source interruption check.
  Freeze already received MIC/render lengths before external speech starts;
  alignment selection and training cannot inspect held-out or mixed samples.
- Validate a fixed far-end transfer on an independent 200 ms window before
  examining raw and residual near-source scores at the same mixed window.
  Reject ambiguous alignment, changed gain/polarity and missing reference.
- Preserve raw NCC/ambiguity gates and reject below-floor raw/residual copies
  that normalized correlation alone could confirm. VAD/AEC/ASR and interruption
  behavior are unchanged.
  Residual matches are numerical diagnostics, not physical double-talk or
  first-audible-onset proof. Cancelled owners cannot publish a stale model;
  no microphone recording is persisted. APK and protocol are unchanged.

## APK 0.9.38 / code75 — recover a stalled streaming tail

- If the speech endpoint is still unplayed and the playback head has stopped
  advancing for 100 ms, allow the existing bounded silence priming path even
  without an explicit pause/resume. Normally advancing playback is unchanged.
- Keep the one-buffer padding cap, cancellation, pause-aware deadline and real
  speech endpoint checks. Padding is never counted as speech/reference audio;
  a permanently stuck track still fails instead of reporting completion.
- Record the prior 0.14.87/code74 playback-ACK timeout before capture. The new
  known-speech observer was not armed in that run; physical cause and the APK
  candidate remain unverified until separate hardware checks.

## Backend 0.14.87 — corroborate one known near speech window

- Add an opt-in, bounded observer of existing capture during a fixed synthetic
  interruption. Match a known 200 ms input window against raw microphone audio,
  reject competing matches and resemblance to the original digital render, and
  report only numerical evidence. Keep VAD, AEC, recognition and interruption
  decisions unchanged.
- Read the hashed public WAV only from an owner-private fixture directory under
  this runtime. Bind observation to the capture epoch and original playback;
  clear buffers on timeout, capture gaps, session/transport changes or completion.
  Perform FFT work on the IPC caller outside the session lock.
- Expose a separate `--known-speech-evidence` check; old backends are rejected
  before device access. A matched window does not measure first audible onset
  or establish physical double talk. APK and wire protocol remain unchanged.

## Device checks — prepare fixed interruption audio before capture

- Synthesize and validate one bounded private WAV before opening capture; play
  that file for the fixed new question or stop phrase, with PCM/file digests
  bound to the preparation receipt. Ordinary wake/question probes keep their
  existing speech path.
- Recheck owner activity, wake mode, transports and APK identity after
  preparation; release the temporary fixture after capture cleanup, including
  preflight failure, partial construction and later test failures.
- Audit optional preparation metadata and cleanup without changing strict
  render-overlap requirements or treating process exit as acoustic evidence.
  Production backend/APK/protocol and voice-processing settings are unchanged.

## Backend 0.14.86 — distinguish pre-VAD playback coverage

- Record separate bounded counters for continuous locally voiced frames before
  VAD begins an utterance. Discard pending flags on silence, clock gaps and
  lifecycle resets, and never retain PCM in this observer.
- Preserve the existing capture counters and strict interruption requirement;
  export prefix coverage for diagnosis without treating it as strict success.
  Older reports retain unknown prefix coverage rather than manufactured zeros.

## Assistant 0.9.37 — reuse a foreground session's output clock

- Keep one warmed AudioTrack across clause leases, with separate physical and
  per-clause frame positions for echo references, captions and playback receipts.
- Preserve applied pause/resume/stop receipts, invalidate old leases before
  clearing late writes, and release output on Stop, background or terminal
  standalone requests. Late PCM cannot create an output owner.
- Cancel the native lease before publishing the Player's stopped flag, so the
  session worker cannot consume the explicit command's applied-stop receipt.
- Keep the mouth closed until the clause playback head advances beyond queued
  warm samples; warm-track playback state alone does not indicate speech.
- Recheck transport identity after control parsing and bind each player and its
  PCM to the original route, including same-generation transport handovers.
- Backend 0.14.85 and protocol 2 remain unchanged. This is an integration
  candidate; device timing, sound, idle resource use and acoustic checks remain.

## Backend 0.14.85 / Assistant 0.9.35 — classify invalid capture clock anchors

- Count the first failed hardware-clock guard by fixed numeric category during
  explicit bounded recording diagnostics. Preserve recording timestamps and
  audio processing. Accept older reports without the optional reason group;
  require complete, bounded counts whose sum equals the rejected total.

## Backend 0.14.84 — pause narration for a standalone listening request

- Treat a standalone “听一下” as a stop-and-listen command in the utterance
  recognition route. Share its command vocabulary with the playback-text check.

## Backend 0.14.83 — record contiguous near-speech and playback overlap

- Include the longest credible frame run and the credible frame counts with and
  without an energetic playback reference in bounded utterance diagnostics.
  Preserve legacy reports with unknown temporal coverage.
- Add an opt-in strict interruption check for energetic playback reference in
  credible near-speech frames, with matching saved-suite audit requirements.

## Backend 0.14.82 — check local response alignment before accepting room compensation

- Locate overlapping short portions of the predicted prefix independently before
  accepting room-based realignment. Preserve an unconfirmed result when local
  timing disagrees despite a strong long-window correlation.

## Backend 0.14.81 — corroborate reflection-sensitive response alignment

- Recheck a small raw anchor disagreement against a room model fitted and
  validated on later audio. Require the modeled prefix to locate the earlier
  audio within the original tolerance and verify every energetic block.

## Backend 0.14.80 — wait for delayed playback reference in the acoustic probe

- Keep the bounded response probe pending when the microphone window arrives
  before the original reference window. Retain the existing match thresholds,
  timeout and discontinuity checks.

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

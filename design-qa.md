# Native CC8 design QA — 0.8.1

Date: 2026-11-01. final result: passed

## Visual truth and scope

- Source: docs/design/bumblebee-orbit-v1.png, the owner-selected first concept with speaking status moved below the waveform.
- Source pixels: 1586×992; normalized with Lanczos to the actual 1280×800 viewport for comparison. No bezel or browser chrome.
- Implementation: real TG_S6B01 / AILABS_S6, SDK 27, density 1, immersive 1280×800; APK 0.8.1 / code 26.
- Screenshot: device-access/private/ui-0.8.0/reference-release-speaking.png.
- Combined full view: device-access/private/ui-0.8.0/reference-release-comparison.png.
- Focused lower halo, waveform, status and controls: device-access/private/ui-0.8.0/reference-release-focus.png.
- Native settings typography: device-access/private/ui-0.8.0/reference-final-settings.png.
- These private device screenshots are local evidence, excluded from Git. This is a native Android application; browser console / CSS checks do not apply.

## State and intentional content differences

Both full-view artifacts show speaking, an open microphone, active wake mode, visible karaoke text, task receipts and the bottom controls. The implementation uses actual playback timestamps and real task data: a read-only reminder query completed, there are no pending reminders, and an explicit local test announcement is playing. It does not create the mock's news file or meeting reminder. Unknown streaming duration displays elapsed time until the actual total is known.

The owner's subsequent font instruction takes precedence over the mock typography: spoken content remains 42sp; auxiliary type is enlarged (buttons 22sp, user transcript / task title 24sp, reminders 23sp, statuses 20sp, dialog body 22sp). This intentional change is included in the pass criteria.

## Comparison history and fixes

1. Initial 0.8.0 used flexible rows with the right column around x558, task divider near y489, and dock near y706. These were P1 differences from the selected composition. It also retained long thin orbit arcs, undersized native icons, and small auxiliary text.
2. 0.8.1 replaced the layout with a 1280×800 reference plane (scaled uniformly with the viewport). Right column x584, task divider y442, reminder divider y583, dock y674/h92. It restores the approved portrait framing, short luminous arcs, radial ticks, dotted orbit, separate waveform and rounded mode group / buttons. Material and Phosphor vector assets replace the small stock icons. Larger auxiliary type and native dialog text are verified on the device.
3. First reference comparison still showed a weak waveform center. The final build adds a cached luminous ridge to the existing audio-driven particle field; the final full and focused comparison shows the stronger center, independent from the portrait and status. Silence does not manufacture energy; pause retains the actual frozen waveform state.
4. Status placement is below the waveform, outside the entire portrait view. Actual view intersection is false; the screen dump reports caption42sp / controls22sp / task24sp. Two real frames confirm motion of the orbit and waveform.

## Required fidelity surfaces

- Fonts / typography: native sans-serif and bold CJK for captions / task title; requested auxiliary sizes verified, no clipping of persistent controls. Long real task titles ellipsize and open in details. Long spoken content scrolls with real word progress.
- Spacing / layout: measured reference positions, full orbit, independent waveform, lower status, fixed task/reminder separators and bottom capsule group are aligned. No extra corner brackets, grid, nested panel or extra main-screen row.
- Colors / tokens: navy base, cyan #43E0FC, amber #FFC75B, pale foreground and muted upcoming words. Controls retain the reference's gradient, outline and rounded grouping.
- Asset fidelity: approved G1 head remains bundled; mouth / eyes animate in its coordinate system. Orbit and waveform use the existing native rendering approach for live states. Icons are official vector contours, with licenses bundled; no placeholder emoji or rasterized UI.
- Copy / content: 大黄蜂 / Bumblebee; actual connection / mic state, actual task receipts, actual reminder data and actual playback progress. Desktop announcements clear unrelated previous citations.

## Primary interactions / verification

- Stock PackageInstaller update, original signing certificate, transferred and installed APK hash verified.
- Both mode buttons tested on the real screen and restored to wake mode; no microphone was opened by these maintenance checks.
- Native task list and text entry opened; IME input Hello submitted successfully and produced a real Dot task.
- Real Dot read-only execution and tool-backed reminder create/list/cancel completed. A scheduled reminder received actual play_done and was removed from the pending list; no temporary pending reminders remain.
- A text task was cancelled through the acknowledged device stop path and remained cancelled.
- Reminder form, settings list and volume dialog opened. No camera was requested: capture count remains zero.
- Normal explicit opening performed one automatic voice start and calibration. Latest announcement play_done completed with the microphone remaining open; app remains foreground, USB selected and paired Wi-Fi ready.
- 247 host tests passed, with 4 missing-private-recording cases skipped. Android assembleRelease and lintRelease passed, zero lint errors. Actual speaking screenshots and playback receipts supplement these checks.

## Remaining P3 / test gaps

- System font antialiasing and standard licensed icon contours have small optical differences from the generated picture; dynamic orbit phase, spoken words, tasks, and reminder contents naturally vary.
- New layout does not expand the prior validation claims for far-field accuracy, physical USB unplug/replug, long-duration standby, or camera hardware. Those remain separate acceptance work.
- Full task history is per Mac service process; reminder delivery depends on Mac service availability and an authenticated foreground CC8 connection.

## Implementation checklist

- [x] Match approved reference layout and preserve approved character.
- [x] Keep status outside halo and waveform.
- [x] Retain spoken42sp and enlarge other UI type.
- [x] Replace actual-data placeholders with real state and provide working controls.
- [x] Compare normalized full screen and focused region after fixes.
- [x] Verify build, tests, signed installation, native interactions, real playback and automatic voice.

final result: passed

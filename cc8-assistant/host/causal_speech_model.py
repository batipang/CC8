"""A held-out far-end transfer check using only pre-stimulus samples.

This is opt-in measurement, never an AEC/VAD input or physical onset claim.
The caller freezes both input lengths before permitting the external stimulus.
No samples after that boundary may select alignment, fit or validate the model.
"""

import numpy as np

from playback_evidence import match_window, block_correlation, MIN_REFERENCE_RMS, MIN_CAPTURE_RMS

# All computations below use decimated 8 kHz samples. Alignment is established
# acoustically; estimated Android/Mac timestamps are not subtracted.
HALF = 48
TRAIN = 1600
GAP = 1600
CHECK = 1600
SPAN = TRAIN + GAP + CHECK
MIN_PRE_FRAMES = 12000


def analyse(mic, render, template, mic_cutoff, render_cutoff):
    """Return numeric diagnostics; a residual match never upgrades the raw gate."""
    result = {
        'schema': 1,
        'status': 'pre_stimulus_incomplete',
        'pre_stimulus_mic_frames': mic_cutoff,
        'pre_stimulus_render_frames': render_cutoff,
        'training_frames': TRAIN * 2,
        'held_out_frames': CHECK * 2,
    }
    if min(mic_cutoff, render_cutoff) < MIN_PRE_FRAMES:
        return result
    before_mic, before_render = mic[:mic_cutoff:2], render[:render_cutoff:2]
    # The fit, validation and alignment windows are fixed before seeing any
    # mixed audio. Reject missing/quiet reference rather than searching later.
    if len(before_render) < SPAN + 2 * HALF:
        return result
    alignment = before_render[HALF : HALF + TRAIN]
    if np.std(alignment) < MIN_REFERENCE_RMS:
        return {**result, 'status': 'reference_below_floor'}
    # Alignment selection sees only the first 400 ms. The held-out target
    # starts at peak + 400 ms or later, so none of it can select the lag.
    peak, confidence, rival, status = match_window(before_mic[: 2 * TRAIN], alignment)
    result.update(alignment_confidence=round(confidence, 4), alignment_rival=round(rival, 4))
    if status != 'observed':
        return {**result, 'status': 'alignment_unconfirmed'}
    # peak is in original 16 kHz frames; the reference window starts at HALF.
    shift = peak // 2 - HALF
    start = HALF + shift
    if start < 0 or start + SPAN > len(before_mic):
        return {**result, 'status': 'pre_stimulus_incomplete'}
    windows = np.lib.stride_tricks.sliding_window_view(
        before_render[: SPAN + 2 * HALF], 2 * HALF + 1
    )[:SPAN]
    target = before_mic[start : start + SPAN]
    x, y = windows[:TRAIN], target[:TRAIN]
    x_mean, y_mean = x.mean(axis=0), y.mean()
    centered = x - x_mean
    gram = centered.T @ centered
    power = np.trace(gram) / len(gram)
    if power < 1e-6:
        return {**result, 'status': 'reference_below_floor'}
    try:
        weights = np.linalg.solve(
            gram + np.eye(len(gram)) * power * 0.05, centered.T @ (y - y_mean)
        )
    except np.linalg.LinAlgError:
        return {**result, 'status': 'fit_unavailable'}
    try:
        bias = y_mean - x_mean @ weights
        held_out = windows[TRAIN + GAP :] @ weights + bias
        measured = target[TRAIN + GAP :]
        score = block_correlation(held_out, measured)
        predicted_power = np.linalg.norm(held_out - held_out.mean())
        actual_power = np.linalg.norm(measured - measured.mean())
        gain_ok = (
            0 < predicted_power and 0.5 * predicted_power <= actual_power <= 2 * predicted_power
        )
        # Correlation is signed here: an inverted held-out transfer is a change,
        # even though the initial NCC intentionally tolerates signal polarity.
        signed = np.dot(held_out - held_out.mean(), measured - measured.mean()) > 0
        result.update(
            held_out_confidence=round(score, 4),
            held_out_gain_valid=bool(gain_ok),
            held_out_polarity_valid=bool(signed),
        )
        if score < 0.8 or not gain_ok or not signed:
            return {**result, 'status': 'held_out_unconfirmed'}
        result['status'] = 'validated'
        if template is None:
            return result
        # Predict only where the same original far-end reference is available.
        # Do not zero-pad the far end and present the remainder as clean speech.
        full = np.lib.stride_tricks.sliding_window_view(render[::2], 2 * HALF + 1)
        left = max((mic_cutoff + 1) // 2, HALF + shift)
        right = min(len(mic) // 2, len(full) + HALF + shift)
        if right - left < len(template) // 2 + 160:
            return {**result, 'status': 'mixed_window_incomplete'}
        predicted = full[left - shift - HALF : right - shift - HALF] @ weights + bias
        residual = mic[::2][left:right] - predicted
        try:
            offset, residual_score, residual_rival, residual_status = match_window(
                residual, template[::2]
            )
            at = offset // 2
            count = len(template) // 2
            raw = mic[::2][left + at : left + at + count]
            near = template[::2]
            residual_rms = float(np.std(residual[at : at + count]))
            # Report raw and residual at the SAME selected window, not two
            # independent best peaks that could refer to different syllables.
            result.update(
                status='residual_observed'
                if residual_status == 'observed' and residual_rms >= MIN_CAPTURE_RMS
                else 'residual_unconfirmed',
                residual_confidence=round(residual_score, 4),
                residual_rival=round(residual_rival, 4),
                raw_at_residual_confidence=round(block_correlation(near, raw), 4),
                predicted_far_rms=round(float(np.std(predicted[at : at + count])), 6),
                residual_rms=round(residual_rms, 6),
                residual_window_offset_frames=(left + at) * 2,
            )
            return result
        finally:
            predicted.fill(0)
            residual.fill(0)
    finally:
        weights.fill(0)

"""Bounded isolated near-source calibration with independent held-out speech."""

import numpy as np

from known_speech_receipt import (
    NEAR_FILTER_HALF,
    NEAR_HOLDOUT_BLOCK,
    NEAR_HOLDOUT_START,
    NEAR_MAX_DELAY,
)
from playback_evidence import MIN_REFERENCE_RMS, match_window, room_block_status

# These are decimated 8 kHz sample indices. The latest training alignment
# ends before 2.207 s of capture, ahead of the earliest 2.244 s holdout.
BLOCK = 80
HALF = NEAR_FILTER_HALF // 2
TRAIN = 8800
MAX_DELAY = NEAR_MAX_DELAY // 2
REFINE = 160
HOLDOUT = NEAR_HOLDOUT_START // 2
CHECK = NEAR_HOLDOUT_BLOCK // 2
REFERENCE_FRAMES = 2 * (HOLDOUT + 2 * CHECK + HALF)
LOW_PASS = np.sinc(0.42 * np.arange(-8, 9)) * np.hamming(17)
LOW_PASS /= LOW_PASS.sum()


def envelope(samples):
    blocks = samples.reshape(-1, BLOCK)
    return np.log1p(np.std(blocks, axis=1) * 1000)


def analyse(reference, microphone):
    """Fit only in a fixed early prefix, then test two later 200 ms blocks."""
    if min(len(reference), len(microphone)) < REFERENCE_FRAMES:
        return {'status': 'coverage_incomplete'}
    ref, mic = (np.convolve(samples, LOW_PASS, 'same')[::2] for samples in (reference, microphone))
    train = ref[HALF : HALF + TRAIN]
    if np.std(train) < MIN_REFERENCE_RMS:
        return {'status': 'training_below_floor'}
    pattern = envelope(train)
    pattern -= pattern.mean()
    windows = np.lib.stride_tricks.sliding_window_view(
        envelope(mic[: TRAIN + MAX_DELAY]), len(pattern)
    )
    centered = windows - windows.mean(axis=1, keepdims=True)
    norm = np.linalg.norm(centered, axis=1) * np.linalg.norm(pattern)
    scores = centered @ pattern / np.maximum(norm, 1e-12)
    peak = int(np.argmax(scores))
    score = float(np.clip(scores[peak], 0, 1))
    scores[max(0, peak - 8) : peak + 9] = 0
    rival = float(np.clip(max(scores), 0, 1))
    result = {'envelope_confidence': round(score, 4), 'envelope_rival': round(rival, 4)}
    if score < 0.60 or score - rival < 0.08:
        return {**result, 'status': 'alignment_unconfirmed'}
    left = max(0, peak * BLOCK - REFINE)
    right = min(MAX_DELAY + HALF, peak * BLOCK + REFINE)
    peak, _, _, _ = match_window(mic[left : right + TRAIN], train)
    start = left + peak // 2
    shift = start - HALF
    result['delay_frames'] = 2 * shift
    if HOLDOUT + shift + 2 * CHECK > len(mic):
        return {**result, 'status': 'coverage_incomplete'}
    # Separate fixed reference slices also prevent later reference samples
    # from affecting the fit. The model never runs on a mixed capture.
    train_windows = np.lib.stride_tricks.sliding_window_view(ref[: TRAIN + 2 * HALF], 2 * HALF + 1)[
        :TRAIN
    ]
    target = mic[start : start + TRAIN]
    mean, bias = train_windows.mean(axis=0), target.mean()
    centered = train_windows - mean
    gram = centered.T @ centered
    power = np.trace(gram) / len(gram)
    if power < 1e-6:
        return {**result, 'status': 'training_below_floor'}
    try:
        weights = np.linalg.solve(
            gram + np.eye(len(gram)) * power * 0.05, centered.T @ (target - bias)
        )
    except np.linalg.LinAlgError:
        return {**result, 'status': 'fit_unavailable'}
    validation = np.lib.stride_tricks.sliding_window_view(
        ref[HOLDOUT - HALF : HOLDOUT + 2 * CHECK + HALF], 2 * HALF + 1
    )[: 2 * CHECK]
    predicted = (validation - mean) @ weights + bias
    measured = mic[HOLDOUT + shift : HOLDOUT + shift + 2 * CHECK]
    checks = [
        room_block_status(predicted[i : i + CHECK], measured[i : i + CHECK], 0.8)
        for i in (0, CHECK)
    ]
    return {
        **result,
        'status': 'validated' if checks == ['matched', 'matched'] else 'held_out_unconfirmed',
        'held_out_blocks': checks,
    }

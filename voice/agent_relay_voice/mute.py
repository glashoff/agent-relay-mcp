"""Mute detection from the microphone signal itself.

Measured with a Jabra Link 380 (2026-11-11), 16-bit samples: with the boom up
the headset sends a constant pattern instead of the microphone (RMS about 0.5,
75 % exact zeros); room noise with the boom down has an RMS of 5-30 and fewer
than 10 % zeros; speech 400-4300; a switched-off headset sends only zeros.

Rule: a block counts as "mute-like" when RMS < rms_threshold and more than
zero_fraction of its samples are exactly zero. Muted after hold_s of
consecutive mute-like blocks; any other block unmutes at once, so the start of
speech after folding the boom down is not lost.
"""

from __future__ import annotations

import numpy as np


def block_stats(block: np.ndarray) -> tuple[float, float]:
    """RMS (in 16-bit sample units) and the fraction of exact zeros."""
    if block.size == 0:
        return 0.0, 1.0
    samples = block.astype(np.float64)
    rms = float(np.sqrt(np.mean(samples * samples)))
    zeros = float(np.count_nonzero(block == 0)) / block.size
    return rms, zeros


class MuteDetector:
    def __init__(self, rms_threshold: float = 1.5, zero_fraction: float = 0.5,
                 hold_s: float = 0.3, sample_rate: int = 16000) -> None:
        self.rms_threshold = rms_threshold
        self.zero_fraction = zero_fraction
        self.hold_s = hold_s
        self.sample_rate = sample_rate
        self._mute_like_s = 0.0
        self.muted = False
        self.last_rms = 0.0
        self.last_zeros = 0.0

    def configure(self, rms_threshold: float, zero_fraction: float, hold_s: float) -> None:
        self.rms_threshold, self.zero_fraction, self.hold_s = rms_threshold, zero_fraction, hold_s

    def is_mute_like(self, rms: float, zeros: float) -> bool:
        return rms < self.rms_threshold and zeros > self.zero_fraction

    def update(self, block: np.ndarray) -> bool:
        """Feed one block of int16 samples; returns whether the mic is muted."""
        rms, zeros = block_stats(block)
        self.last_rms, self.last_zeros = rms, zeros
        if self.is_mute_like(rms, zeros):
            self._mute_like_s += block.size / self.sample_rate
            # small tolerance for float sums of 0.1 s blocks
            if self._mute_like_s >= self.hold_s - 1e-6:
                self.muted = True
        else:
            self._mute_like_s = 0.0
            self.muted = False
        return self.muted
